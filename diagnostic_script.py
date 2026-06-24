import pandas as pd
import numpy as np

def run_diagnostics():
    path_14a = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_14A.parquet"
    path_13a = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_13A.parquet"

    print("Loading datasets...")
    try:
        df_14a = pd.read_parquet(path_14a)
        print(f"Loaded 14A: {df_14a.shape}")
    except Exception as e:
        print(f"Error loading 14A: {e}")
        return

    try:
        df_13a = pd.read_parquet(path_13a)
        print(f"Loaded 13A: {df_13a.shape}")
    except Exception as e:
        print(f"Error loading 13A: {e}")
        df_13a = None

    print("\n" + "="*50)
    print("1. Chronological Location")
    print("="*50)
    if df_13a is not None:
        if isinstance(df_13a.index, pd.DatetimeIndex):
            print(f"HourSet_13A min date: {df_13a.index.min()}")
            print(f"HourSet_13A max date: {df_13a.index.max()}")
        else:
            print("13A index is not DatetimeIndex. Columns:", df_13a.columns[:5])
            if 'date' in df_13a.columns:
                print(f"HourSet_13A min date: {df_13a['date'].min()}")
                print(f"HourSet_13A max date: {df_13a['date'].max()}")
            elif 'timestamp' in df_13a.columns:
                print(f"HourSet_13A min date: {df_13a['timestamp'].min()}")
                print(f"HourSet_13A max date: {df_13a['timestamp'].max()}")

    if isinstance(df_14a.index, pd.DatetimeIndex):
        print(f"\nHourSet_14A min date: {df_14a.index.min()}")
        print(f"HourSet_14A max date: {df_14a.index.max()}")
    else:
        print("\n14A index is not DatetimeIndex. Columns:", df_14a.columns[:5])
        if 'date' in df_14a.columns:
            print(f"HourSet_14A min date: {df_14a['date'].min()}")
            print(f"HourSet_14A max date: {df_14a['date'].max()}")
        elif 'timestamp' in df_14a.columns:
            print(f"HourSet_14A min date: {df_14a['timestamp'].min()}")
            print(f"HourSet_14A max date: {df_14a['timestamp'].max()}")

    print("\n" + "="*50)
    print("2. NaN Count in HourSet_14A")
    print("="*50)
    nan_counts = df_14a.isna().sum().sort_values(ascending=False).head(20)
    print(nan_counts)

    print("\n" + "="*50)
    print("4. The Backfill / Zero-Fill Audit")
    print("="*50)
    ovx_cols = [c for c in df_14a.columns if 'OVX' in c.upper()]
    if not ovx_cols:
        print("Could not find any column with 'OVX' in the name.")
        print("Some macro columns:", [c for c in df_14a.columns if 'MACRO' in c.upper()][:10])
    else:
        ovx_col = ovx_cols[0]
        print(f"Using column: {ovx_col}")
        
        # Chronologically oldest 100 rows
        # Check if index is datetime or we need to sort by a date column
        if not isinstance(df_14a.index, pd.DatetimeIndex):
            date_col = 'date' if 'date' in df_14a.columns else 'timestamp' if 'timestamp' in df_14a.columns else None
            if date_col:
                df_14a = df_14a.sort_values(by=date_col)
        else:
            df_14a = df_14a.sort_index()

        oldest_100 = df_14a.head(100)
        print(oldest_100[ovx_col].values)

if __name__ == '__main__':
    run_diagnostics()
