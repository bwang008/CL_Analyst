import pandas as pd

# Load the file
path = "data/processed/CL_set_01.parquet"  # Adjust if your filename is different
print(f"Loading {path}...")
df = pd.read_parquet(path)

# 1. Check Dimensions
print(f"\nDimensions: {df.shape}")
print(f"Rows: {len(df):,}")
print(f"Columns: {len(df.columns)}")

# 2. Check the Targets
print("\n--- Target Inspection ---")
if 'TARGET_SQUEEZE' in df.columns:
    print("TARGET_SQUEEZE Found!")
    print(df['TARGET_SQUEEZE'].value_counts(dropna=False))
else:
    print("WARNING: TARGET_SQUEEZE missing!")

# 3. Check the Tail (The Int64 Verification)
print("\n--- Last 5 Rows (Checking for <NA>) ---")
print(df[['Close', 'TARGET_Direction', 'TARGET_SQUEEZE']].tail())

# 4. Check for Numba Features
print("\n--- Physics Layer Check ---")
if 'STRUC_HURST_100' in df.columns:
    print(f"Hurst Exponent Mean: {df['STRUC_HURST_100'].mean():.4f}")
else:
    print("WARNING: Hurst Exponent missing!")