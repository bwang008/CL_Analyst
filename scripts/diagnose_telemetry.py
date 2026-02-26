"""Quick diagnostic: check telemetry DB for signal history and bar data."""
import sqlite3
import pandas as pd

conn = sqlite3.connect("data/live_telemetry.db")

# Check table sizes
print("=== Table Sizes ===")
for t in ["market_bars", "raw_front_month_bars", "trade_ledger"]:
    cur = conn.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"  {t}: {cur.fetchone()[0]} rows")

# Recent signals
print("\n=== Recent Signals (last 20) ===")
df = pd.read_sql(
    "SELECT timestamp, signal, probability, action_taken "
    "FROM trade_ledger ORDER BY id DESC LIMIT 20",
    conn,
)
if len(df) > 0:
    print(df.to_string(index=False))
else:
    print("  (no signals recorded)")

# Probability distribution
print("\n=== Probability Distribution ===")
df_prob = pd.read_sql(
    "SELECT COUNT(*) as n, "
    "MIN(probability) as min_prob, "
    "AVG(probability) as avg_prob, "
    "MAX(probability) as max_prob "
    "FROM trade_ledger WHERE probability IS NOT NULL",
    conn,
)
print(df_prob.to_string(index=False))

# Recent bars (continuous)
print("\n=== Recent Market Bars (last 5) ===")
df2 = pd.read_sql(
    "SELECT timestamp, open, high, low, close, volume "
    "FROM market_bars ORDER BY id DESC LIMIT 5",
    conn,
)
if len(df2) > 0:
    print(df2.to_string(index=False))
else:
    print("  (no bars recorded)")

# Raw front-month bars
print("\n=== Recent Raw Front-Month Bars (last 5) ===")
df3 = pd.read_sql(
    "SELECT timestamp, open, high, low, close, volume, contract_month "
    "FROM raw_front_month_bars ORDER BY id DESC LIMIT 5",
    conn,
)
if len(df3) > 0:
    print(df3.to_string(index=False))
else:
    print("  (no raw front-month bars recorded)")

# Time range of bars
print("\n=== Bar Time Range ===")
for t in ["market_bars", "raw_front_month_bars"]:
    try:
        r = conn.execute(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM {t}"
        ).fetchone()
        print(f"  {t}: {r[0]} → {r[1]}")
    except Exception:
        print(f"  {t}: (empty)")

conn.close()
