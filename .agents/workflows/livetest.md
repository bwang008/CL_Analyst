---
description: Run a livetest simulation to validate the LiveTrader against the BacktestEngine
---

// turbo-all

# LiveTest Workflow

The **livetest engine** replays historical OHLCV bars through the full, unmodified **LiveTrader** production pipeline using dependency-injected mock adapters (`SimulatedDataFeed`, `SimulatedExecution`). It produces a trade ledger that can be compared against the BacktestEngine.

## What Is This For?

| Use Case | Mode | Description |
|----------|------|-------------|
| **Execution Parity Proof** | `--predictions-dir` | Inject the backtest's exact prediction CSVs to bypass live inference. Proves the matching engine, TP/SL, trailing stops, and bracket orders produce identical trades. Use this for **regression testing** after code changes. |
| **Reality Gap Assessment** | Standard (no flags) | Run with live feature generation + LightGBM inference. Quantifies how much PnL, win rate, and trade count degrade from backtest → live due to incremental vs batch feature computation drift. |
| **Pre-Deployment Validation** | Standard | Run over 6-12 months to produce the **true expected live performance baseline** before going live with real money. |

## Performance Expectations

| Data Slice | Total Bars | Warmup | Replay | Wall Clock |
|------------|------------|--------|--------|------------|
| 1 month | ~2,920 | 2,200 | ~720 | **~10 min** |
| 3 months | ~4,360 | 2,200 | ~2,160 | **~30 min** |
| 6 months | ~6,520 | 2,200 | ~4,320 | **~60 min** |
| 12 months | ~10,920 | 2,200 | ~8,720 | **~2.5 hours** |

> **Rule of thumb**: ~1.5 bars/sec for hourly models (measured 2026-07-04: 336-bar parity replay in 3.6 min). Warmup is fast (batch); replay runs full inference per bar.
>
> **Note**: per-bar cost scales with the daily FRED history length in `fred_macro_data_<symbol>.csv`, since macro features are rebuilt over the full history each bar. A slow-lambda percentile implementation made this ~10.6 s/bar (~0.06 bars/sec) until it was replaced with native `Rolling.rank` on 2026-07-04 (ticket `livetest-macro-pctile-slow_07042026_1748`). If throughput regresses far below this table, profile with py-spy before shrinking the replay window.

## Step 1: Prepare a Data Subset

Create a subset with enough warmup (≥2,200 bars for hourly models to satisfy MACRO_3M and VOL_ROC_10080 indicators) plus the desired replay window.

```powershell
# 1-month subset (2200 warmup + 720 replay = 2920 bars)
python -c "import pandas as pd; df = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet'); subset = df.iloc[-2920:]; subset.to_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_11_tiny.parquet'); print(f'Saved: {len(subset)} bars, {subset.index[0]} -> {subset.index[-1]}')"

# 12-month subset (2200 warmup + 8720 replay = 10920 bars)
python -c "import pandas as pd; df = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet'); subset = df.iloc[-10920:]; subset.to_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_11_12m.parquet'); print(f'Saved: {len(subset)} bars, {subset.index[0]} -> {subset.index[-1]}')"
```

Adjust the tail slice to control the replay window size.

## Step 2: Run the Livetest

### Standard Mode (Live Inference)

Runs the full pipeline: feature generation → LightGBM → simulated execution.

```powershell
python scripts/livetest_engine.py --config configs/strategies/HS11_Prod_Ensemble_E01_06162026.json --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_11_tiny.parquet" --warmup-bars 2200 --output reports/livetest_trades_HS11.csv --slippage-per-side 0.01 --progress-every 200 --log-level INFO
```

### Parity Mode (Prediction Injection)

Bypasses live inference and injects pre-computed prediction CSVs from the backtest. Used for regression testing and execution parity proofs.

```powershell
python scripts/livetest_engine.py --config configs/strategies/HS11_Prod_Ensemble_E01_06162026.json --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_11_tiny.parquet" --warmup-bars 2200 --predictions-dir reports/ --output reports/livetest_trades_HS11_parity.csv --slippage-per-side 0.01 --log-level INFO
```

The `--predictions-dir` flag points to the base directory. The engine resolves the actual CSV paths from the strategy config's `models.long.predictions_path` and `models.short.predictions_path` fields, relative to this base directory.

### CLI Arguments Reference

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--config` | Yes | — | Path to strategy config JSON |
| `--data` | Yes | — | Path to historical data parquet |
| `--exec-data` | No | None | Raw/panama CSV for split-brain execution pricing |
| `--warmup-bars` | No | 15000 | Bars for indicator warmup (use ≥2200 for hourly) |
| `--output` | No | `reports/livetest_trades.csv` | Path for trade ledger CSV output |
| `--slippage-per-side` | No | 0.01 | Slippage per side in price units |
| `--progress-every` | No | 500 | Log progress interval (bars) |
| `--log-level` | No | INFO | Log verbosity: DEBUG/INFO/WARNING/ERROR |
| `--predictions-dir` | No | None | Base path for prediction CSVs (enables parity mode) |

## Step 3: Verify the Run Is Clean

After the run completes, check for warnings:

```powershell
Select-String -Path "<log_file_path>" -Pattern "WARNING" | Where-Object { $_.Line -notmatch "Cache depth" }
```

**Expected**: Zero non-cache warnings. If you see any of these, there's a bug:
- `TRAILING STOP: triggered but _sl_order_id is None` → bracket children not placed
- `SL order not found in open trades` → `_open_orders` not populated with child orders
- `BRACKET CHILDREN: no decision context` → decision context dict key mismatch

## Step 4: Generate Backtest Trades for Comparison

### For Parity Mode (deterministic comparison)

**CRITICAL**: Run WITHOUT `--exec-data`. Both systems must use the same OHLCV source to achieve deterministic parity.

```powershell
python agent/backtest_engine.py --config configs/strategies/HS11_Prod_Ensemble_E01_06162026.json --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet" --slippage-per-side 0.01
```

### For Standard Mode (realistic comparison)

Use `--exec-data` for realistic split-brain execution pricing:

```powershell
python agent/backtest_engine.py --config configs/strategies/HS11_Prod_Ensemble_E01_06162026.json --data "C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet" --exec-data "C:\CL_Analyst_Data\data\raw\DataBentoSample\CL_raw.csv" --slippage-per-side 0.01
```

Filter `reports/backtest_trades_HS11.csv` to the replay date range for comparison.

## Step 5: Reconcile Trades

Compare the trade ledgers side by side:

1. **Entry fill prices** — In parity mode: must match exactly ($0.00 delta). In standard mode: may differ due to feature drift.
2. **Exit reasons** — Note the naming mapping:
   - Backtest `SL` → Livetest `SL_HIT`
   - Backtest `TP` → Livetest `TP_HIT`
   - Backtest `TRAILING_BE` → Livetest `SL_HIT` (trailing stop tightened SL, then SL filled)
   - `TIME_BARRIER` → `TIME_BARRIER` (same in both)
3. **Trade count** — In parity mode: must match exactly. In standard mode: may differ due to prediction drift.
4. **PnL per matched trade** — In parity mode: expect ≤$5.00 delta (tick-size rounding). In standard mode: divergence is expected.

## Known Architectural Differences

These are structural differences between BacktestEngine and LiveTrader, not bugs:

1. **Tick-size rounding**: ConfigurableStrategy rounds TP/SL prices with `round(price, 2)` (correct for CL's $0.01 tick size). BacktestEngine does NOT round. This causes exit fills to differ by up to $0.005, resulting in ≤$5.00 PnL delta per trade. The livetest is more correct here.

2. **Trailing stop evaluation order**: Both systems use N+1 evaluation — the OLD SL is checked against bar N, trailing activation updates the SL, and the new SL only takes effect on bar N+1. Verified by code tracing.

3. **Feature computation**: BacktestEngine pre-computes all features in one vectorized pass. LiveTrader builds features incrementally via the rolling cache. Long-window features (Ichimoku, MACRO_3M) may have small floating-point differences that occasionally flip signals across the probability threshold.

4. **Prediction coverage**: BacktestEngine uses pre-computed prediction CSVs with a fixed date range. LiveTrader generates predictions on-the-fly, so it may start trading earlier if warmup covers enough data.

## Key Files

| File | Purpose |
|------|---------|
| `scripts/livetest_engine.py` | Main simulation driver |
| `src/live_execution/adapters/simulated_execution.py` | Matching engine + execution mock |
| `src/live_execution/adapters/simulated_data_feed.py` | Bar replay mock |
| `src/live_execution/live_trader.py` | Production LiveTrader (DO NOT MODIFY for tests) |
| `agent/backtest_engine.py` | Vectorized backtester |
| `tests/test_simulated_execution.py` | 32 unit tests for the matching engine |

## Adapting for Other Configs

To run a livetest against a different strategy config (e.g., a new HourSet or ensemble):

1. Replace the `--config` path with the new config JSON
2. Replace the `--data` path with the correct parquet for that config's dataset
3. Ensure the parquet has enough bars for warmup (≥2,200 for hourly models)
4. For parity mode: ensure the config's `models.long.predictions_path` and `models.short.predictions_path` point to valid prediction CSVs relative to `--predictions-dir`
