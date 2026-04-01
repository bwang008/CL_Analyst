import zipfile
import re

zf_path = 'tmp/canary_artifacts.zip'

print("=== 5-Minute Models (set_11c) found in GCP Zip ===")
print(f"{'Experiment ID':<45} | {'Trades':>6} | {'WR%':>5} | {'PF':>5} | {'PnL':>11}")
print("-" * 80)

try:
    with zipfile.ZipFile(zf_path) as zf:
        for name in zf.namelist():
            if 'set_11c' in name and name.endswith('backtest_report.txt'):
                content = zf.read(name).decode('utf-8')
                
                # Extract basic stats
                trades = "0"
                wr = "0.0"
                pf = "0.00"
                pnl = "$0.00"
                
                for line in content.split('\n'):
                    if 'Trade Count:' in line:
                        trades = line.split(':')[-1].strip()
                    elif 'Win Rate:' in line:
                        wr = line.split(':')[-1].strip().replace('%', '')
                    elif 'Profit Factor:' in line:
                        pf = line.split(':')[-1].strip()
                    elif 'Total PnL:' in line:
                        pnl = line.split(':')[-1].strip()
                
                exp_id = name.split('/')[2]
                print(f"{exp_id:<45} | {trades:>6} | {wr:>5} | {pf:>5} | {pnl:>11}")
                
except Exception as e:
    print(f"Error accessing zip: {e}")
