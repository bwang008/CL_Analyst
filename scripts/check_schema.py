"""Check DB schema."""
import sqlite3
conn = sqlite3.connect("data/live_telemetry.db")
for table in ["market_bars", "raw_front_month_bars", "trade_ledger"]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    print(f"\n{table}:")
    for r in cur.fetchall():
        print(f"  {r}")
conn.close()
