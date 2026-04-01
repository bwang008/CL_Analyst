"""Check telemetry DB contents for stitch feasibility."""
import sqlite3
import os
import sys

# Check multiple DB files
db_candidates = [
    "data/live_telemetry.db",
    "data/live_telemetry_cid10.db",
    "data/live_telemetry_cid13.db",
]

for db_path in db_candidates:
    if not os.path.exists(db_path):
        print(f"{db_path}: NOT FOUND")
        continue
    sz = os.path.getsize(db_path)
    print(f"\n{'='*60}")
    print(f"{db_path}: {sz/1024/1024:.2f} MB")
    print(f"{'='*60}")
    
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    print(f"Tables: {tables}")
    
    for t in tables:
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM [{}]".format(t)).fetchone()[0]
            if cnt > 0:
                # Get date range
                ts_col = "timestamp" if t != "tradebook_events" else "event_timestamp_utc"
                try:
                    rng = conn.execute(
                        "SELECT MIN({c}), MAX({c}) FROM [{t}]".format(c=ts_col, t=t)
                    ).fetchone()
                    print(f"  {t}: {cnt:,} rows  [{rng[0]} -> {rng[1]}]")
                except:
                    print(f"  {t}: {cnt:,} rows")
            else:
                print(f"  {t}: {cnt} rows (empty)")
        except Exception as e:
            print(f"  {t}: ERROR - {e}")
    
    # Check market_bars sample if it has data
    if "market_bars" in tables:
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0]
            if cnt > 0:
                print(f"\n  market_bars sample (first 3):")
                rows = conn.execute("SELECT * FROM market_bars ORDER BY timestamp LIMIT 3").fetchall()
                for r in rows:
                    print(f"    {r}")
                print(f"  market_bars sample (last 3):")
                rows = conn.execute("SELECT * FROM market_bars ORDER BY timestamp DESC LIMIT 3").fetchall()
                for r in rows:
                    print(f"    {r}")
        except:
            pass
    
    # Check raw_front_month_bars sample
    if "raw_front_month_bars" in tables:
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM raw_front_month_bars").fetchone()[0]
            if cnt > 0:
                print(f"\n  raw_front_month_bars sample (first 3):")
                rows = conn.execute("SELECT * FROM raw_front_month_bars ORDER BY timestamp LIMIT 3").fetchall()
                for r in rows:
                    print(f"    {r}")
                print(f"  raw_front_month_bars sample (last 3):")
                rows = conn.execute("SELECT * FROM raw_front_month_bars ORDER BY timestamp DESC LIMIT 3").fetchall()
                for r in rows:
                    print(f"    {r}")
                # Contract months
                months = conn.execute("SELECT DISTINCT contract_month FROM raw_front_month_bars ORDER BY contract_month").fetchall()
                print(f"  Contract months: {[m[0] for m in months]}")
        except:
            pass
    
    # Check shadow_log for prob data
    if "shadow_log" in tables:
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM shadow_log").fetchone()[0]
            if cnt > 0:
                print(f"\n  shadow_log sample (last 3):")
                rows = conn.execute(
                    "SELECT timestamp, open, high, low, close, volume, prob_buy, prob_sell, strategy_name "
                    "FROM shadow_log ORDER BY timestamp DESC LIMIT 3"
                ).fetchall()
                for r in rows:
                    print(f"    {r}")
        except:
            pass
    
    conn.close()

# Also check CL_DATA_ROOT
data_root = os.environ.get("CL_DATA_ROOT", "")
if data_root:
    print(f"\nCL_DATA_ROOT: {data_root}")
    for db_path in db_candidates:
        alt = os.path.join(data_root, db_path.replace("data/", ""))
        if os.path.exists(alt):
            print(f"  Found: {alt} ({os.path.getsize(alt)/1024/1024:.2f} MB)")
