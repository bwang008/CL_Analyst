---
description: Run a livetest simulation to validate parity between LiveTrader and BacktestEngine
---

// turbo-all

# LiveTest Simulation Workflow

This workflow runs historical data through the full **LiveTrader** pipeline (feature generation → LightGBM inference → simulated execution) and compares the results against the BacktestEngine to verify parity.

## Performance Expectations

| Data Slice | Bars | Warmup | Replay | Wall Clock |
|------------|------|--------|--------|------------|
| 1 month (hourly) | ~2,920 | 2,200 | ~720 | **~10 min** |
| 3 months (hourly) | ~4,360 | 2,200 | ~2,160 | **~30 min** |
| 6 months (hourly) | ~6,520 | 2,200 | ~4,320 | **~60 min** |

> **Rule of thumb**: ~1 bar/sec for hourly models. The warmup phase is fast (batch); the replay phase runs full inference per bar.

## Step 1: Prepare a data subset

The livetest does not need the full dataset. Create a subset with enough warmup (≥2,200 bars for hourly models to satisfy MACRO_3M and VOL_ROC_10080 indicators) plus the desired replay window.

```powershell
# Create a 1-month subset (2200 warmup + 720 replay = 2920 bars)
python -c "import pandas as pd; df = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet'); subset = df.iloc[-2920:]; subset.to_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_11_tiny.parquet'); print(f'Saved: {len(subset)} bars, {subset.index[0]} -> {subset.index[-1]}')"
```

Adjust `-2920` to control the replay window size. For 3 months use `-4360`, for 6 months use `-6520`.

## Step 2: Run the livetest engine

```powershell
python scripts/livetest_engine.py --config configs/strategies/HS11_Prod_Ensemble_E01_06162026.json --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_11_tiny.parquet" --warmup-bars 2200 --output reports/livetest_trades_HS11.csv --slippage-per-side 0.01 --progress-every 200 --log-level INFO 2>&1
```

### CLI Arguments Reference

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config` | Yes | — | Path to strategy config JSON |
| `--data` | Yes | — | Path to historical data parquet |
| `--exec-data` | No | None | Raw/panama CSV for split-brain execution |
| `--warmup-bars` | No | 4500 | Bars for indicator warmup (use ≥2200 for hourly) |
| `--output` | No | `reports/livetest_trades.csv` | Path for trade ledger CSV output |
| `--slippage-per-side` | No | 0.01 | Slippage per side in price units |
| `--progress-every` | No | 500 | Log progress interval (bars) |
| `--log-level` | No | INFO | Log verbosity: DEBUG/INFO/WARNING/ERROR |

## Step 3: Verify the run is clean

After the run completes, check for warnings:

```powershell
Select-String -Path "<log_file_path>" -Pattern "WARNING" | Where-Object { $_.Line -notmatch "Cache depth" }
```

**Expected**: Zero non-cache warnings. If you see any of these, there's a bug:
- `TRAILING STOP: triggered but _sl_order_id is None` → bracket children not placed
- `SL order not found in open trades` → _open_orders not populated with child orders
- `BRACKET CHILDREN: no decision context` → decision context dict key mismatch

## Step 4: Run the backtest for the same period

```powershell
python agent/backtest_engine.py --config configs/strategies/HS11_Prod_Ensemble_E01_06162026.json --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet" --exec-data "C:\CL_Analyst_Data\data\raw\DataBentoSample\CL_raw.csv" --slippage-per-side 0.01
```

This produces the full backtest. To export trades as CSV for comparison, use the helper script or filter the CSV output by date range matching the replay window.

## Step 5: Reconcile trades

Compare the trade ledgers side by side:

1. **Entry fill prices** — Should match exactly when entry times align
2. **Exit reasons** — Note that naming differs:
   - Backtest: `SL`, `TP`, `TRAILING_BE`, `TIME_BARRIER`
   - Livetest: `SL_HIT`, `TP_HIT`, `TIME_BARRIER`
   - `TRAILING_BE` in backtest maps to `SL_HIT` in livetest (trailing stop tightens SL, then SL fills)
3. **Trade count** — May differ due to incremental vs batch feature computation causing prediction drift
4. **PnL per matched trade** — Expect <$500 delta on matched trades (trailing stop timing differences)

## Known Parity Differences

These are architectural differences between BacktestEngine and LiveTrader, not bugs:

1. **Trailing stop evaluation order**: Both systems use N+1 evaluation — the OLD SL is checked against bar N, trailing activation updates the SL, and the new SL only takes effect on bar N+1. This was verified by code tracing (BacktestEngine L692-745, LiveTrader replay loop steps 1-3). Any trailing stop PnL differences are due to minor fill price rounding, not timing.

2. **Feature computation**: BacktestEngine pre-computes all features on the full dataset in one shot. LiveTrader builds features incrementally via the rolling cache. Long-window features (Ichimoku, MACRO_3M) may have small floating-point differences that accumulate over time, occasionally flipping signals across the probability threshold.

3. **Prediction coverage**: BacktestEngine uses pre-computed prediction CSVs with a fixed date range. LiveTrader generates predictions on-the-fly from the feature cache, so it may start trading earlier if warmup covers enough data.
