import pandas as pd
import os

input_file = r'C:\CL_Analyst_Data\data\raw\DataBentoSample\adjusted_CL_history.csv'
output_file = r'C:\CL_Analyst_Data\data\raw\CL.csv'

print(f"Reading from {input_file}...")
df = pd.read_csv(input_file)

print("Parsing ts_event...")
ts = pd.to_datetime(df['ts_event'])
df['Date'] = ts.dt.strftime('%d/%m/%Y')  # The loader uses '%d/%m/%Y %H:%M'
df['Time'] = ts.dt.strftime('%H:%M')

print("Renaming columns...")
rename_map = {'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}
df = df.rename(columns=rename_map)

print("Reordering columns and dropping extras...")
cols = ['Date', 'Time', 'Open', 'High', 'Low', 'Close', 'Volume']
df = df[cols]

# Ensure directory exists
os.makedirs(os.path.dirname(output_file), exist_ok=True)

# Save
print(f"Saving to {output_file}...")
df.to_csv(output_file, index=False, header=False, sep=';')
print("Done!")
