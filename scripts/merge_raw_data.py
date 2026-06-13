import pandas as pd
import os
import shutil

raw_data_dir = r"C:\CL_Analyst_Data\data\raw"
old_file_path = os.path.join(raw_data_dir, "cl-5m_mar30.csv")
new_file_path = os.path.join(raw_data_dir, "cl-5m_bk_jun26.csv")
output_file_path = os.path.join(raw_data_dir, "cl-5m_bk.csv")
cl_csv_path = os.path.join(raw_data_dir, "CL.csv")

print("Loading old data...")
old_df = pd.read_csv(old_file_path, sep=";", header=None)
old_df.columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume"]

old_df['datetime'] = pd.to_datetime(old_df['Date'] + ' ' + old_df['Time'], format="%d/%m/%Y %H:%M:%S", errors='coerce')
if old_df['datetime'].isnull().all():
    old_df['datetime'] = pd.to_datetime(old_df['Date'] + ' ' + old_df['Time'], format="%d/%m/%Y %H:%M", errors='coerce')

print("Loading new data...")
new_df = pd.read_csv(new_file_path, sep=";", header=None)
new_df.columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume"]

new_df['datetime'] = pd.to_datetime(new_df['Date'] + ' ' + new_df['Time'], format="%d/%m/%Y %H:%M:%S", errors='coerce')
if new_df['datetime'].isnull().all():
    new_df['datetime'] = pd.to_datetime(new_df['Date'] + ' ' + new_df['Time'], format="%d/%m/%Y %H:%M", errors='coerce')

overlap_time = old_df['datetime'].iloc[-1]
print(f"Overlap time (end of old data): {overlap_time}")

overlap_old = old_df[old_df['datetime'] == overlap_time]
overlap_new = new_df[new_df['datetime'] == overlap_time]

if not overlap_old.empty and not overlap_new.empty:
    old_close = overlap_old['Close'].iloc[-1]
    new_close = overlap_new['Close'].iloc[-1]
    
    # RATIO ADJUSTMENT: Scale old history to match new baseline
    factor = new_close / old_close
    print(f"Old Close: {old_close}, New Close: {new_close}")
    print(f"Applying ratio factor of: {factor:.8f} to old data")
    
    for col in ['Open', 'High', 'Low', 'Close']:
        old_df[col] = old_df[col] * factor
else:
    print("Warning: Could not find exact overlap timestamp.")

# Take old_df up to overlap_time, and new_df after overlap_time
old_prior_df = old_df.copy()
new_post_df = new_df[new_df['datetime'] > overlap_time].copy()

# Standardize Date and Time format
old_prior_df['Date'] = old_prior_df['datetime'].dt.strftime('%d/%m/%Y')
old_prior_df['Time'] = old_prior_df['datetime'].dt.strftime('%H:%M')
new_post_df['Date'] = new_post_df['datetime'].dt.strftime('%d/%m/%Y')
new_post_df['Time'] = new_post_df['datetime'].dt.strftime('%H:%M')

old_prior_df = old_prior_df.drop(columns=['datetime'])
new_post_df = new_post_df.drop(columns=['datetime'])

old_prior_df['Volume'] = old_prior_df['Volume'].astype(int).astype(str)
new_post_df['Volume'] = new_post_df['Volume'].astype(int).astype(str)

for col in ['Open', 'High', 'Low', 'Close']:
    old_prior_df[col] = old_prior_df[col].apply(lambda x: f"{x:.6f}")
    new_post_df[col] = new_post_df[col].apply(lambda x: f"{x:.6f}")

print("Concatenating data...")
merged_df = pd.concat([old_prior_df, new_post_df], ignore_index=True)
merged_df = merged_df[["Date", "Time", "Open", "High", "Low", "Close", "Volume"]]

print(f"Saving merged data to {output_file_path}...")
merged_df.to_csv(output_file_path, sep=";", header=False, index=False)
print("Done saving.")

print(f"Creating a copy as {cl_csv_path}...")
shutil.copy2(output_file_path, cl_csv_path)
print("Ratio Merge and copy completed successfully.")
