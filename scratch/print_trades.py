import pandas as pd
ldf = pd.read_csv('reports/livetest_trades_HS11_parity.csv')
bdf = pd.read_csv('reports/backtest_trades_HS11.csv')
start = ldf['entry_time'].min()
end = ldf['entry_time'].max()
bdf = bdf[(bdf['entry_time'] >= start) & (bdf['entry_time'] <= end)].copy()
ldf = ldf.sort_values('entry_time').reset_index(drop=True)
bdf = bdf.sort_values('entry_time').reset_index(drop=True)
print('Live count:', len(ldf))
print('BT count:', len(bdf))
for i in range(min(len(ldf), len(bdf))):
    if ldf.iloc[i]['entry_time'] != bdf.iloc[i]['entry_time']:
        print(f"Mismatch at {i}: Live={ldf.iloc[i]['entry_time']} BT={bdf.iloc[i]['entry_time']}")
        break
print('Live head:')
print(ldf[['entry_time', 'exit_time', 'prob']].head(5))
print('BT head:')
print(bdf[['entry_time', 'exit_time', 'prob']].head(5))
