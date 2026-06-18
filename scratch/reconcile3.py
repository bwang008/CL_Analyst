import pandas as pd
import math

livetest_df = pd.read_csv('reports/livetest_trades_HS11_parity.csv')
backtest_df = pd.read_csv('reports/backtest_trades_patched.csv')

# Only compare trades from 2026-04-30 04:00:00 onwards
livetest_df = livetest_df[livetest_df['entry_time'] >= '2026-04-30 04:00:00'].copy()
backtest_df = backtest_df[backtest_df['entry_time'] >= '2026-04-30 04:00:00'].copy()

livetest_df = livetest_df.sort_values('entry_time').reset_index(drop=True)
backtest_df = backtest_df.sort_values('entry_time').reset_index(drop=True)

print(f'Livetest Trades: {len(livetest_df)}')
print(f'Backtest Trades: {len(backtest_df)}')

mapping = {
    'SL': 'SL_HIT',
    'TP': 'TP_HIT',
    'TRAILING_BE': 'SL_HIT',
    'TIME_BARRIER': 'TIME_BARRIER'
}

diffs = 0
for i in range(min(len(livetest_df), len(backtest_df))):
    live = livetest_df.iloc[i]
    bt = backtest_df.iloc[i]
    
    bt_exit_mapped = mapping.get(bt['exit_reason'], bt['exit_reason'])
    
    mismatch = False
    if bt_exit_mapped != live['exit_reason']:
        print(f"[{i}] Exit Reason mismatch: BT={bt['exit_reason']} -> {bt_exit_mapped} vs Live={live['exit_reason']}")
        mismatch = True
        
    if not math.isclose(live['entry_fill'], bt['entry_fill'], abs_tol=0.01):
        print(f"[{i}] Entry Fill mismatch: Live={live['entry_fill']} vs BT={bt['entry_fill']}")
        mismatch = True
        
    if not math.isclose(live['exit_fill'], bt['exit_fill'], abs_tol=0.01):
        print(f"[{i}] Exit Fill mismatch: Live={live['exit_fill']} vs BT={bt['exit_fill']}")
        mismatch = True
        
    if not math.isclose(live['net_pnl_dollars'], bt['net_pnl_dollars'], abs_tol=0.01):
        print(f"[{i}] PnL mismatch: Live={live['net_pnl_dollars']} vs BT={bt['net_pnl_dollars']}")
        mismatch = True

    if live['entry_time'] != bt['entry_time']:
        print(f"[{i}] Entry Time mismatch: Live={live['entry_time']} vs BT={bt['entry_time']}")
        mismatch = True

    if mismatch:
        diffs += 1
        
if diffs == 0 and len(livetest_df) == len(backtest_df):
    print('100% mathematical parity achieved! All trades match exactly.')
else:
    print(f'Reconciliation completed with {diffs} differences.')
