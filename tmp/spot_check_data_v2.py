import pandas as pd
import numpy as np
import os

files = {
    'new': r'C:\CL_Analyst_Data\data\raw\cl-5m_bk.csv',
    'feb26': r'C:\CL_Analyst_Data\data\raw\cl-5m_bk_feb26.csv',
    'dec25': r'C:\CL_Analyst_Data\data\raw\cl-5m_bk_dec25.csv'
}

dfs = {}
out_lines = []

for name, path in files.items():
    if not os.path.exists(path):
        out_lines.append(f"File NOT FOUND: {name} at {path}")
        continue
        
    out_lines.append(f"Loading {name} ({os.path.basename(path)})...")
    df = pd.read_csv(path, sep=';', header=None, 
                     names=['Date','Time','Open','High','Low','Close','Volume'])
    
    # Check format and parse. The newest one might have seconds again, the others don't.
    sample_time = str(df.iloc[0]['Time'])
    if len(sample_time.split(':')) == 3:
        fmt = '%d/%m/%Y %H:%M:%S'
    else:
        fmt = '%d/%m/%Y %H:%M'
        
    df['DateTime'] = pd.to_datetime(df['Date'] + ' ' + df['Time'], format=fmt)
    df.set_index('DateTime', inplace=True)
    dfs[name] = df
    
    out_lines.append(f"  {name} loaded: {len(df):,} rows")
    out_lines.append(f"  Range: {df.index.min()} -> {df.index.min()}") # BUG! I'll just write it correctly below
    out_lines[-1] = f"  Range: {df.index.min()} -> {df.index.max()}"

# Let's compare new vs feb26 first
if 'new' in dfs and 'feb26' in dfs:
    df_new = dfs['new']
    df_old = dfs['feb26']
    
    common_idx = df_new.index.intersection(df_old.index)
    
    if len(common_idx) > 0:
        out_lines.append("\n" + "="*50)
        out_lines.append(f"Comparison: NEW vs FEB26 ({len(common_idx):,} overlapping rows)")
        
        np.random.seed(42)
        sample_idx = np.random.choice(common_idx, 3, replace=False)
        sample_idx = sorted(sample_idx)
        
        for ts in sample_idx:
            n_row = df_new.loc[ts]
            o_row = df_old.loc[ts]
            if isinstance(n_row, pd.DataFrame): n_row = n_row.iloc[0]
            if isinstance(o_row, pd.DataFrame): o_row = o_row.iloc[0]
                
            out_lines.append(f"\nTimestamp: {ts}")
            out_lines.append(f"  NEW:   C={n_row.Close:<8} V={n_row.Volume}")
            out_lines.append(f"  FEB26: C={o_row.Close:<8} V={o_row.Volume}")
            diff = abs(n_row.Close - o_row.Close)
            if diff > 0.01:
                out_lines.append(f"  [!] MISMATCH: diff = {diff:.4f}")
            else:
                out_lines.append("  [OK] Exact Match!")
                
        # Global stat check
        overlap_new = df_new.loc[common_idx]
        overlap_old = df_old.loc[common_idx]
        overlap_new = overlap_new[~overlap_new.index.duplicated(keep='last')]
        overlap_old = overlap_old[~overlap_old.index.duplicated(keep='last')]
        diff_c = (overlap_new['Close'] - overlap_old['Close']).abs()
        out_lines.append(f"\nGlobal Max Price Diff (New vs Feb26): {diff_c.max():.4f}")
        out_lines.append(f"Rows with > 0.01 Diff: {(diff_c > 0.01).sum():,}")
    else:
         out_lines.append("\nNo overlap between NEW and FEB26.")

# Let's compare feb26 vs dec25
if 'feb26' in dfs and 'dec25' in dfs:
    df_feb = dfs['feb26']
    df_dec = dfs['dec25']
    
    common_idx = df_feb.index.intersection(df_dec.index)
    
    if len(common_idx) > 0:
        out_lines.append("\n" + "="*50)
        out_lines.append(f"Comparison: FEB26 vs DEC25 ({len(common_idx):,} overlapping rows)")
        
        # We don't need line by line, just global stat
        overlap_feb = df_feb.loc[common_idx]
        overlap_dec = df_dec.loc[common_idx]
        overlap_feb = overlap_feb[~overlap_feb.index.duplicated(keep='last')]
        overlap_dec = overlap_dec[~overlap_dec.index.duplicated(keep='last')]
        diff_c = (overlap_feb['Close'] - overlap_dec['Close']).abs()
        out_lines.append(f"Global Max Price Diff (Feb26 vs Dec25): {diff_c.max():.4f}")
        out_lines.append(f"Rows with > 0.01 Diff: {(diff_c > 0.01).sum():,}")

with open('tmp/spot_check_report_v2.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out_lines))
print("\nDone. Saved to tmp/spot_check_report_v2.txt")
