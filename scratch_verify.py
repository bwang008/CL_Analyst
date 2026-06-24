import pandas as pd
import sys

def verify_datasets():
    path_new = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_14A.parquet"
    path_old = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_14A_backup.parquet"
    
    print("Loading new dataset...")
    df_new = pd.read_parquet(path_new)
    print(f"New shape: {df_new.shape}")
    
    print("Loading old dataset...")
    df_old = pd.read_parquet(path_old)
    print(f"Old shape: {df_old.shape}")
    
    print("Comparing datasets...")
    try:
        pd.testing.assert_frame_equal(df_new, df_old)
        print("SUCCESS: The datasets are exactly identical!")
    except AssertionError as e:
        print("FAILURE: The datasets are NOT identical.")
        print(e)
        sys.exit(1)

if __name__ == "__main__":
    verify_datasets()
