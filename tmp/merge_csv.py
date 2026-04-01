import pandas as pd
import numpy as np

print("Loading OLD data (cl-5m_bk.csv)...")
df_old = pd.read_csv(r'C:\CL_Analyst_Data\data\raw\cl-5m_bk.csv', sep=';', header=None, 
                     names=['Date','Time','Open','High','Low','Close','Volume'])
# Parse datetime (format: DD/MM/YYYY HH:MM)
df_old['DateTime'] = pd.to_datetime(df_old['Date'] + ' ' + df_old['Time'], format='%d/%m/%Y %H:%M')
print(f"Old data loaded: {len(df_old):,} rows")
print(f"  Range: {df_old['DateTime'].min()} -> {df_old['DateTime'].max()}")

print("\nLoading NEW data (cl-5m_mar30.csv)...")
df_new = pd.read_csv(r'C:\CL_Analyst_Data\data\raw\cl-5m_mar30.csv', sep=';', header=None,
                     names=['Date','Time','Open','High','Low','Close','Volume'])
# Parse datetime (format: DD/MM/YYYY HH:MM:SS)
df_new['DateTime'] = pd.to_datetime(df_new['Date'] + ' ' + df_new['Time'], format='%d/%m/%Y %H:%M:%S')
print(f"New data loaded: {len(df_new):,} rows")
print(f"  Range: {df_new['DateTime'].min()} -> {df_new['DateTime'].max()}")

print("\nMerging and deduping...")
# Combine
df_combined = pd.concat([df_old, df_new], ignore_index=True)

# Sort by datetime so newer rows come last
df_combined = df_combined.sort_values('DateTime')

# Drop duplicates based on DateTime, keeping the LAST one (which will be from the new file if there's overlap)
# The new file is generally better quality as it's a fresh export
df_combined = df_combined.drop_duplicates(subset=['DateTime'], keep='last')
print(f"Combined data: {len(df_combined):,} unique rows")
print(f"  Range: {df_combined['DateTime'].min()} -> {df_combined['DateTime'].max()}")

# Ensure the Time column is formatted back to HH:MM (without seconds) strictly for the output
# The pipeline relies on this format
df_combined['Date_Out'] = df_combined['DateTime'].dt.strftime('%d/%m/%Y')
df_combined['Time_Out'] = df_combined['DateTime'].dt.strftime('%H:%M')

df_out = df_combined[['Date_Out', 'Time_Out', 'Open', 'High', 'Low', 'Close', 'Volume']]

out_path = r'C:\CL_Analyst_Data\data\raw\cl-5m_bk_merged.csv'
print(f"\nSaving consolidated file to {out_path}...")
df_out.to_csv(out_path, sep=';', header=False, index=False)
print("Done!")
