---
description: Standard template for presenting canary/experiment backtest results to the user
---

# Model Run Report Template

When presenting backtest or canary results to the user, **always** use the standardized table format below.
This ensures consistency across sessions and makes it easy to compare models.

## Required Columns

Every model result report **must** include these columns in this order:

| Column | Description | Example |
|---|---|---|
| **Model Name** | Experiment ID or descriptive name | `EXP-037 LeanMomentum Short` |
| **ML Metric** | Optimization metric used by Optuna | `logloss`, `average_precision` |
| **Best ML Score** | Best value achieved for that metric | `-0.6904` |
| **Trades** | Total OOS trade count | `208` |
| **Win Rate** | Percentage of winning trades | `31.2%` |
| **Profit Factor** | Gross profit / gross loss | `1.27` |
| **PnL** | Net PnL after commissions + slippage | `+$5,816` |
| **Max Drawdown** | Peak-to-trough drawdown in dollars | `-$6,445` |
| **OOS Period** | Out-of-sample date range | `2022-01 to 2026-02` |
| **Contract** | Futures contract used in backtest | `CL ($1,000/pt)` or `MCL ($100/pt)` |
| **Direction** | Trading direction | `Short`, `Long`, `Both`, `Directionless` |

## Example Table

```markdown
| Model Name | ML Metric | Best ML Score | Trades | WR | PF | PnL | Max DD | OOS Period | Contract | Direction |
|---|---|---:|---:|---:|---:|---:|---:|---|---|---|
| EXP-037 LeanMomentum | logloss | -0.6904 | 208 | 31.2% | 1.27 | +$5,816 | -$6,445 | 2022-01 to 2026-02 | CL | Short |
```

## Notes

- **Contract**: The BacktestEngine defaults to `contract_multiplier=1000.0` (full CL). If MCL is used, set to 100.0 and note `MCL ($100/pt)`.
- **Max Drawdown**: Always extracted from `BacktestResult.max_drawdown`. Available in all backtest report `.txt` files.
- **Best ML Score**: Pull from canary log output — the "Best value" line from Optuna. For logloss, values are negative (lower = better).
- **OOS Period**: Derived from the OOS predictions CSV index date range.
- **Direction**: Use the target suffix to determine — `_LONG` = Long, `_SHORT` = Short, `_MULTI` = Both, no suffix = Directionless.

## When to Use

Use this template in:
1. `walkthrough.md` artifacts when reporting experiment results
2. Responses to user questions about model performance
3. Canary result summaries after GCP runs complete
4. `experiment_tracker.json` updates (map columns to JSON fields)
