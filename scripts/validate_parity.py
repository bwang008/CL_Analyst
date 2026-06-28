import pandas as pd
import sys
from src.data_paths import get_data_path

def main():
    print("Loading Original Dataset...")
    df_old = pd.read_parquet(get_data_path("processed/CL_HourSet_14A_backup.parquet"))
    
    print("Loading Config-Driven Dataset...")
    df_new = pd.read_parquet(get_data_path("processed/CL_HourSet_14A.parquet"))
    
    print(f"Original shape: {df_old.shape}")
    print(f"New shape: {df_new.shape}")
    
    if df_old.shape != df_new.shape:
        print("ERROR: Shape mismatch!")
        # Let's see what columns are different
        old_cols = set(df_old.columns)
        new_cols = set(df_new.columns)
        print(f"Columns in old but not new: {old_cols - new_cols}")
        print(f"Columns in new but not old: {new_cols - old_cols}")
        sys.exit(1)
        
    try:
        pd.testing.assert_frame_equal(df_old, df_new, check_like=True)
        print("\n[SUCCESS] Datasets are bit-for-bit identical (ignoring column ordering)!")
    except AssertionError as e:
        print(f"\n[ERROR] Datasets are NOT identical. Difference details:\n{e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
