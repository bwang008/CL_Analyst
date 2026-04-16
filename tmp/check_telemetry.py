"""Analyze signal distribution from live telemetry."""
import sqlite3, pandas as pd

conn = sqlite3.connect(r"C:\CL_Analyst_Data\data\live_telemetry_cid25.db")

# Get all trade_ledger signals with probabilities
ledger = pd.read_sql("SELECT * FROM trade_ledger ORDER BY timestamp", conn)
print(f"Total signals logged: {len(ledger)}")
print(f"Date range: {ledger['timestamp'].min()} -> {ledger['timestamp'].max()}")
print(f"\nSignal distribution:")
print(ledger['signal'].value_counts())
print(f"\nAction distribution:")
print(ledger['action_taken'].value_counts())

# Look at probability range
print(f"\nConfidence stats:")
print(ledger['confidence_pct'].describe())

# Shadow log - look at buy/sell probs 
shadow = pd.read_sql("SELECT * FROM shadow_log ORDER BY timestamp", conn)
print(f"\n--- Shadow log ({len(shadow)} rows) ---")
print(shadow.columns.tolist())
prob_cols = [c for c in shadow.columns if 'prob' in c.lower()]
if prob_cols:
    print(f"\nProb columns: {prob_cols}")
    print(shadow[['timestamp'] + prob_cols].describe())
    print("\nLast 10 prob values:")
    print(shadow[['timestamp'] + prob_cols].tail(10).to_string())

conn.close()
