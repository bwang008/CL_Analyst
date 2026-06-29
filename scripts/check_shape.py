import pandas as pd
from pandas.testing import assert_frame_equal

print("Loading legacy 14A backup...")
df_old = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_14A_backup.parquet')

print("Loading newly generated 14A...")
df_new = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_14A.parquet')

print(f"Legacy 14A shape: {df_old.shape}")
print(f"New 14A shape: {df_new.shape}")

# Filter both down to just the feature columns (excluding TARGET_*)
features_old = [c for c in df_old.columns if not c.startswith('TARGET_')]
features_new = [c for c in df_new.columns if not c.startswith('TARGET_')]

print(f"Legacy 14A Feature Columns: {len(features_old)}")
print(f"New 14A Feature Columns: {len(features_new)}")

# Align column order for exact comparison
df_old_feat = df_old[features_old].sort_index(axis=1)
df_new_feat = df_new[features_new].sort_index(axis=1)

try:
    assert_frame_equal(df_old_feat, df_new_feat, check_dtype=False)
    print("SUCCESS: 100% Feature Column and Value Parity Achieved!")
except AssertionError as e:
    print("Assertion failed!")
    print(e)
