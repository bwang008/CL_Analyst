import re

with open('reports/batch_runs/batch_20260618_1721_HS13A_SCOUT_370/sharpe_ensemble_backtests.md', 'r') as f:
    text = f.read()

m = re.search(r'## Ensemble 4.*?(Total Net PnL:\s*\$[^\\n]+).*?(HOLDOUT REPORT.*?Total Net PnL:\s*\$[^\\n]+)', text, re.DOTALL)
if m:
    print('Ensemble 4 Backtest:')
    print('  ' + m.group(1).strip())
    holdout_pnl = re.search(r'Total Net PnL:\s*\$([\\d,\\.-]+)', m.group(2))
    print('  Holdout PnL:', holdout_pnl.group(1).strip())

m2 = re.search(r'## Ensemble 1.*?(Total Net PnL:\s*\$[^\\n]+).*?(HOLDOUT REPORT.*?Total Net PnL:\s*\$[^\\n]+)', text, re.DOTALL)
if m2:
    print('\nEnsemble 1 Backtest:')
    print('  ' + m2.group(1).strip())
    holdout_pnl2 = re.search(r'Total Net PnL:\s*\$([\\d,\\.-]+)', m2.group(2))
    print('  Holdout PnL:', holdout_pnl2.group(1).strip())
