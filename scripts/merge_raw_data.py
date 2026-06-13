import pandas as pd
import numpy as np
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

first_new_time = new_df['datetime'].iloc[0]
print(f"New data starts at: {first_new_time}")

old_prior_df = old_df[old_df['datetime'] < first_new_time].copy()
print(f"Isolated {len(old_prior_df)} rows from old data to append.")

overlap_old = old_df[old_df['datetime'] == first_new_time]
if not overlap_old.empty:
    old_close = overlap_old['Close'].iloc[0]
    new_close = new_df['Close'].iloc[0]
    shift = new_close - old_close
    print(f"Applying price shift of: {shift:.5f}")
    for col in ['Open', 'High', 'Low', 'Close']:
        old_prior_df[col] = old_prior_df[col] + shift

# Standardize Date and Time to match the %d/%m/%Y and %H:%M format exactly
old_prior_df['Date'] = old_prior_df['datetime'].dt.strftime('%d/%m/%Y')
old_prior_df['Time'] = old_prior_df['datetime'].dt.strftime('%H:%M')
new_df['Date'] = new_df['datetime'].dt.strftime('%d/%m/%Y')
new_df['Time'] = new_df['datetime'].dt.strftime('%H:%M')

old_prior_df = old_prior_df.drop(columns=['datetime'])
new_df_to_save = new_df.drop(columns=['datetime'])

old_prior_df['Volume'] = old_prior_df['Volume'].astype(int).astype(str)
new_df_to_save['Volume'] = new_df_to_save['Volume'].astype(int).astype(str)

for col in ['Open', 'High', 'Low', 'Close']:
    old_prior_df[col] = old_prior_df[col].apply(lambda x: f"{x:.6f}")
    new_df_to_save[col] = new_df_to_save[col].apply(lambda x: f"{x:.6f}")

print("Concatenating data...")
merged_df = pd.concat([old_prior_df, new_df_to_save], ignore_index=True)

# Important: ensure column order matches expectations
merged_df = merged_df[["Date", "Time", "Open", "High", "Low", "Close", "Volume"]]

print(f"Saving merged data to {output_file_path}...")
merged_df.to_csv(output_file_path, sep=";", header=False, index=False)
print("Done saving.")

print(f"Creating a copy as {cl_csv_path}...")
shutil.copy2(output_file_path, cl_csv_path)
print("Merge and copy completed successfully.")
