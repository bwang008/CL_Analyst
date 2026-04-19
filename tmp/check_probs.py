import sqlite3
import pandas as pd
import sys
import os

sys.path.insert(0, r"c:\Users\bwang\Documents\GitHub\CL_Analyst_Development")
from src.data_paths import get_data_path

db_path = get_data_path("live_telemetry.db")
print("Connecting to DB:", db_path)

try:
    conn = sqlite3.connect(db_path)
    # Get shadow_log entries from the last ~2-3 days
    # (Since we just want the recent ones, we can just grab the last 150 rows)
    df = pd.read_sql("SELECT timestamp, strategy_name, prob_buy, prob_sell FROM shadow_log ORDER BY timestamp DESC LIMIT 150", conn)
    
    # Let's filter to the period after the fix (approx April 16/17 onwards)
    print(f"\nTotal recent rows fetched: {len(df)}")
    if len(df) > 0:
        print(f"Time range: {df['timestamp'].min()} to {df['timestamp'].max()}")
        print(f"\nFirst 5 recent predictions:\n{df.head(5)}")
        
        print("\n--- Summary Statistics ---")
        print(df[['prob_buy', 'prob_sell']].describe())
        
        # Count how many were above the 0.50 and 0.55 thresholds
        buy_over_50 = (df['prob_buy'] >= 0.50).sum()
        buy_over_55 = (df['prob_buy'] >= 0.55).sum()
        sell_over_50 = (df['prob_sell'] >= 0.50).sum()
        sell_over_55 = (df['prob_sell'] >= 0.55).sum()
        
        print(f"\nThreshold analysis over last {len(df)} hourly/5m bars:")
        print(f"Buy signals >= 0.50: {buy_over_50}")
        print(f"Buy signals >= 0.55: {buy_over_55}")
        print(f"Sell signals >= 0.50: {sell_over_50}")
        print(f"Sell signals >= 0.55: {sell_over_55}")
        print("Note: Before the fix, max probability was capped at ~0.497")
        
except Exception as e:
    print("Error:", e)
finally:
    if 'conn' in locals():
        conn.close()
