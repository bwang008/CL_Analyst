"""Check the shared CL_DATA_ROOT telemetry DBs."""
import sqlite3
import os

db_dir = r"C:\CL_Analyst_Data\data"
for fname in sorted(os.listdir(db_dir)):
    if not fname.endswith(".db"):
        continue
    path = os.path.join(db_dir, fname)
    sz = os.path.getsize(path)
    print(f"\n--- {fname} ({sz/1024/1024:.2f} MB) ---")
    
    conn = sqlite3.connect(path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    
    for t in tables:
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM [{}]".format(t)
            ).fetchone()[0]
            if cnt > 0:
                ts_col = "event_timestamp_utc" if t == "tradebook_events" else "timestamp"
                try:
                    rng = conn.execute(
                        "SELECT MIN([{}]), MAX([{}]) FROM [{}]".format(ts_col, ts_col, t)
                    ).fetchone()
                    print("  {} = {} rows".format(t, cnt))
                    print("    from: {}".format(rng[0]))
                    print("    to:   {}".format(rng[1]))
                except:
                    print("  {} = {} rows".format(t, cnt))
            else:
                print("  {} = 0 rows".format(t))
        except:
            pass
    conn.close()
