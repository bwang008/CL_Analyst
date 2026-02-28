# Short Concurrent Tuning Report (2026-02-27)

## Overview

This report summarizes short-side concurrent backtesting and grid tuning using `prob_Sell` signals with the concurrent backtester. Ranking metric: `pnl_to_drawdown_ratio = total_pnl / abs(max_drawdown)`.

Data note: runs used `data/processed/CL_set_06_shortfix.parquet` but fell back to `data/raw/cl-5m_bk.csv` for OHLCV columns.

## Tested Parameter Ranges

Position sizing ON:

- Coarse grid: threshold {0.60, 0.65, 0.70} x TP {5, 7} x SL {0.75, 1.0}
- Refined grid: threshold {0.58, 0.60, 0.62} x TP {4.5, 5.0, 5.5} x SL {0.75, 1.0}

Position sizing OFF (flat):

- Small grid: threshold {0.60, 0.65} x TP {5} x SL {0.75, 1.0}

Note: an earlier broader coarse grid was terminated due to runtime; the reduced coarse grid above completed.

## Top Parameter Sets (Refined, Position Sizing ON)


| Rank | Threshold | TP  | SL   | Trades | Win Rate | Profit Factor | Total PnL     | Max Drawdown | PnL/DD | Max Concurrent |
| ---- | --------- | --- | ---- | ------ | -------- | ------------- | ------------- | ------------ | ------ | -------------- |
| 1    | 0.58      | 4.5 | 0.75 | 12,518 | 28.2%    | 2.06          | $2,048,905.92 | -$120,992.65 | 16.93  | 28             |
| 2    | 0.58      | 5.0 | 0.75 | 12,518 | 25.7%    | 2.06          | $2,076,607.99 | -$124,440.44 | 16.69  | 30             |
| 3    | 0.60      | 4.5 | 0.75 | 7,889  | 32.0%    | 2.30          | $1,952,830.31 | -$119,199.91 | 16.38  | 23             |
| 4    | 0.60      | 5.0 | 0.75 | 7,889  | 29.2%    | 2.30          | $1,985,987.80 | -$121,843.35 | 16.30  | 23             |
| 5    | 0.58      | 5.0 | 1.0  | 12,518 | 28.2%    | 2.25          | $2,459,256.86 | -$165,940.58 | 14.82  | 34             |
| 6    | 0.58      | 4.5 | 1.0  | 12,518 | 31.0%    | 2.26          | $2,428,449.86 | -$164,352.37 | 14.78  | 32             |
| 7    | 0.60      | 5.5 | 0.75 | 7,889  | 26.6%    | 2.26          | $1,959,989.26 | -$132,934.44 | 14.74  | 23             |
| 8    | 0.58      | 5.5 | 0.75 | 12,518 | 23.4%    | 2.03          | $2,051,004.13 | -$143,400.36 | 14.30  | 31             |
| 9    | 0.60      | 5.0 | 1.0  | 7,889  | 31.6%    | 2.46          | $2,264,449.02 | -$159,188.18 | 14.22  | 28             |
| 10   | 0.62      | 4.5 | 0.75 | 5,035  | 35.2%    | 2.59          | $1,622,044.85 | -$115,827.35 | 14.00  | 23             |


## Best Configuration (By PnL/DD)

- Threshold: 0.58
- TP: 4.5x ATR
- SL: 0.75x ATR
- Position sizing: ON
- PnL/DD: 16.93
- Total PnL: $2.05M, Max DD: -$121.0K, Max Concurrent: 28

## Baseline Comparison (Short, Flat Sizing)

Baseline (flat sizing, threshold=0.60, TP=5.0, SL=0.75):

- PnL/DD: 14.35
- Total PnL: $0.96M
- Max DD: -$67.1K

Best tuned (sizing ON, threshold=0.58, TP=4.5, SL=0.75):

- PnL/DD: 16.93
- Total PnL: $2.05M
- Max DD: -$121.0K

Observation: position sizing roughly doubles PnL with higher drawdown; the ratio still improves vs flat.

## Aligned Data Check (Flat Sizing)

Re-run on aligned processed OHLCV (no raw fallback) with friction:

- Threshold 0.60, TP 5.0x, SL 0.75x
- Total PnL: $962,957.72
- Max DD: -$67,112.51
- PnL/DD: 14.35

## Recommendation

Production candidate for short-side concurrent use:

- Threshold 0.58
- TP 4.5x ATR
- SL 0.75x ATR
- Position sizing ON (default tiers)

If risk limits prioritize lower drawdown, a conservative alternative is:

- Threshold 0.60
- TP 5.0x ATR
- SL 0.75x ATR
- Position sizing ON

## Outputs

CSV sweep outputs (all saved in `reports/`):

- `short_concurrent_grid_coarse_ps.csv`
- `short_concurrent_grid_refined_ps.csv`
- `short_concurrent_grid_flat.csv`

