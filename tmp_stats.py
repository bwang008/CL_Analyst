import pandas as pd
df = pd.read_csv("reports/backtest_results.csv")
trades = len(df)
net = df['net_pnl_dollars'].sum() if 'net_pnl_dollars' in df.columns else df['pnl'].sum()
gross = df['gross_pnl_dollars'].sum()
avg_gross = gross / trades if trades else 0
col = 'exit_reason' if 'exit_reason' in df.columns else 'reason'
flips = (df[col].astype(str) == 'ExitReason.STRATEGY_FLIP').sum()

print(f"Trades: {trades}")
print(f"Net PnL: ${net:,.2f}")
print(f"Gross PnL: ${gross:,.2f}")
print(f"Avg Gross Edge: ${avg_gross:,.2f}")
print(f"Flips: {flips}")
