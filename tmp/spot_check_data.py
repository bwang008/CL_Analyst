import pandas as pd
import numpy as np

print("Loading OLD data (cl-5m_bk.csv)...")
df_old = pd.read_csv(r'C:\CL_Analyst_Data\data\raw\cl-5m_bk.csv', sep=';', header=None, 
                     names=['Date','Time','Open','High','Low','Close','Volume'])
df_old['DateTime'] = pd.to_datetime(df_old['Date'] + ' ' + df_old['Time'], format='%d/%m/%Y %H:%M')

print("Loading NEW data (cl-5m_mar30.csv)...")
df_new = pd.read_csv(r'C:\CL_Analyst_Data\data\raw\cl-5m_mar30.csv', sep=';', header=None,
                     names=['Date','Time','Open','High','Low','Close','Volume'])
df_new['DateTime'] = pd.to_datetime(df_new['Date'] + ' ' + df_new['Time'], format='%d/%m/%Y %H:%M:%S')

# Set index to DateTime for both to easily join and compare
df_old.set_index('DateTime', inplace=True)
df_new.set_index('DateTime', inplace=True)

# Find the intersection of timestamps
common_idx = df_old.index.intersection(df_new.index)
print(f"\nFound {len(common_idx):,} overlapping timestamps between the two files.")

if len(common_idx) == 0:
    print("No overlapping timestamps found! Cannot compare.")
    exit(1)

# Grab a random sample of 5 timestamps to display
np.random.seed(42)
sample_idx = np.random.choice(common_idx, 5, replace=False)
sample_idx = sorted(sample_idx)

out_lines = []
out_lines.append("\n--- Spot Check Comparison ---")
for ts in sample_idx:
    old_row = df_old.loc[ts]
    new_row = df_new.loc[ts]
    
    # If the index isn't unique, just take the first row for display
    if isinstance(old_row, pd.DataFrame): old_row = old_row.iloc[0]
    if isinstance(new_row, pd.DataFrame): new_row = new_row.iloc[0]
        
    out_lines.append(f"\nTimestamp: {ts}")
    out_lines.append(f"  OLD: O={old_row.Open:<6} H={old_row.High:<6} L={old_row.Low:<6} C={old_row.Close:<6} V={old_row.Volume}")
    out_lines.append(f"  NEW: O={new_row.Open:<6} H={new_row.High:<6} L={new_row.Low:<6} C={new_row.Close:<6} V={new_row.Volume}")
    
    # Check for exact matches
    mismatches = []
    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
        if not np.isclose(old_row[col], new_row[col], rtol=1e-5):
            mismatches.append(col)
            
    if mismatches:
        out_lines.append(f"  [!] MISMATCH IN: {', '.join(mismatches)}")
    else:
        out_lines.append("  [OK] Exact Match!")

# Do a global statistics check on the overlap
out_lines.append("\n--- Global Overlap Statistics ---")
overlap_old = df_old.loc[common_idx]
overlap_new = df_new.loc[common_idx]

# Drop duplicate indices if any before comparison
overlap_old = overlap_old[~overlap_old.index.duplicated(keep='last')]
overlap_new = overlap_new[~overlap_new.index.duplicated(keep='last')]

diff_close = (overlap_old['Close'] - overlap_new['Close']).abs()
out_lines.append(f"Close Price Max Difference: {diff_close.max():.4f}")
out_lines.append(f"Close Price > 0.01 Diff: {(diff_close > 0.01).sum():,} rows")

diff_vol = (overlap_old['Volume'] - overlap_new['Volume']).abs()
out_lines.append(f"Volume Max Difference: {diff_vol.max()}")
out_lines.append(f"Volume > 0 Diff: {(diff_vol > 0).sum():,} rows")

with open('tmp/spot_check_report.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out_lines))
print("Done. Report saved to tmp/spot_check_report.txt.")


