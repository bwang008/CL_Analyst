# Agent Task: Investigate Train-Serve Feature Skew in CL_Analyst Live Trader

## Objective

Identify ALL sources of train-serve skew (feature mismatch between training-time and live-inference-time computation) in the CL_Analyst live trading pipeline. Produce a definitive list of issues that must be fixed before retraining. **Do not fix anything yet** — this is a diagnostic-only task. A second agent will apply all fixes and retrain.

## Background & Context

The live trader (`src/live_execution/live_trader.py`) uses two LightGBM models (EXP-032 SHORT, EXP-033 LONG) with 154 features each. Both models use `use_focal=True` (binary focal loss), meaning the LightGBM Booster returns raw logits that need sigmoid transform. The strategy (`src/live_execution/strategies/configurable_strategy.py`) correctly applies sigmoid in `_run_inference()`.

### The Problem

In OOS backtesting, both models produce signals (prob >= 0.60) on **99.8% of trading days**, averaging ~50 signals/day. In live trading, the models have gone a full day producing **zero signals** — max probabilities around 0.48-0.55, never reaching the 0.60 threshold.

### What We've Already Verified (Do NOT Re-Investigate)

A previous agent exhaustively verified these components — they are confirmed working:

1. **Cache integrity**: Panama Canal $0.03 back-adjustment is correct. CID18 backup vs current cache produces 0 features differing >1% at the same timestamp
2. **Feature count**: All 154 features are generated (0 missing)
3. **Sigmoid transform**: Correctly applied for focal-loss models
4. **Telemetry accuracy**: 20/20 bars match exactly between telemetry and model re-run
5. **Cache depth**: MACRO features are identical at all depths (10K-35K bars) — warm-up depth is NOT an issue
6. **Pre-rollover signal reproduction**: `build_live_features` on the current cache at Mar 17 20:20 produces sell=0.98, matching pre-rollover telemetry

### Known Issue #1: Resample Lookahead Bias in MACRO Features

**Location**: `src/features/alpha_factory.py`, method `add_macro_context()` (line 389-413)

```python
hourly = self.df.resample("1h").agg(ohlcv).dropna()
# ...Donchian on hourly...
macro = macro.reindex(self.df.index, method="ffill")
```

**The bug**: In training, `resample("1h")` creates complete hourly bars (all 12 five-min bars). The `10:00` hourly bar knows the High/Low through `10:55`. `reindex(method="ffill")` then assigns this to the `10:05` bar — leaking 50 minutes of future data. In live, the DataFrame ends at the current bar, so no lookahead exists. This creates train-serve skew.

**Magnitude**: ~4% leak for MACRO_1D (24h), ~0.04% for MACRO_3M (2160h).

**Important caveat**: This leak existed BEFORE the rollover fix and was present when the model WAS producing sell=0.99 signals (March 13-18). So it alone cannot explain why signals stopped. There may be additional skew sources.

**Proposed fix** (for the second agent): Replace the hourly resample with a causally-safe bar-level rolling window (e.g., `840 hours × 12 = 10,080 bars`).

## Your Investigation Tasks

### Task 1: Feature-by-Feature Train vs Live Comparison

Compare the EXACT feature values from the OOS training predictions against the live telemetry features to identify which features have drifted in scale or distribution.

**Data sources**:
- OOS predictions: `models/registry/EXP-033_.../oos_predictions.csv` (has DateTime + prob_Buy)
- OOS predictions: `models/registry/EXP-032_.../oos_predictions.csv` (has DateTime + prob_Sell)
- Live telemetry: `C:\CL_Analyst_Data\data\live_telemetry_cid18.db` (SQLite, table `shadow_log`, has `features_json` column with all 154 features as JSON)
- Training datasets: Check `C:\CL_Analyst_Data\data\processed\` for the set_08 processed parquet files

The OOS predictions CSVs may not have feature values. If not, you'll need to:
1. Load the set_08 training data
2. Regenerate features using `AlphaFactory.add_all_features(windows=[288,864,2016,4032,10080], include_extended=True, macro_windows={"1D":24,"3D":72,"1W":168,"2W":336,"1M":840,"3M":2160})`
3. Compare the feature distributions against the telemetry features

**What to look for**: Features where the live distribution (from telemetry) is significantly different from the training distribution. Compute mean, std, min, max, percentiles for each feature in both contexts. Flag features where the live mean falls outside the training [5th, 95th] percentile range.

### Task 2: NaN/Fill Handling Skew

Compare how NaN values are handled in training vs live:

**Training pipeline** (`src/data_processing/data_processor.py`):
- How does the DataProcessor handle NaN, inf, edge-of-window values?
- Does it drop rows with NaN? Forward-fill? Backfill? Fill with 0?
- What happens to the first N rows where rolling windows haven't warmed up?

**Live pipeline** (`src/live_execution/live_trader.py`, `build_live_features()` at line 293-301):
```python
work.replace([np.inf, -np.inf], np.nan, inplace=True)
work.ffill(inplace=True)
work.bfill(inplace=True)
work.fillna(0, inplace=True)
```

**What to look for**: If training drops the first N warm-up rows (where features are NaN) but live uses bfill/fillna(0), the model never saw zero-filled features during training. This could cause unexpected behavior.

### Task 3: Additional Resample/Lookahead Audit

Audit ALL feature computation in `alpha_factory.py` for any other potential lookahead:
- Are there any other `resample()` calls?
- Do any features use future data via `shift(-N)` or similar?
- Are there any `.rolling()` calls with `center=True`?
- Does `pandas_ta` (used for RSI, ADX, MACD, BBands) introduce any lookahead?

### Task 4: Mar 17 vs Mar 20 Deep Dive

The model produced sell=0.99 on March 17 but only sell=0.48 on March 20 using the same pipeline. Determine if this is purely a market regime difference or if there's a data issue:

1. Load the cache, truncate to Mar 17 20:20, run `build_live_features`, extract all 154 feature values
2. Load the cache, truncate to Mar 20 19:20, run `build_live_features`, extract all 154 feature values
3. Compare feature-by-feature — which features changed the most?
4. Use the model's feature importance to determine if the most-changed features are also the most important to the model's decisions
5. Conclusion: is the model's behavior change explained by legitimate feature changes, or is there artificial suppression?

## Key Files

| File | Purpose |
|------|---------|
| `src/features/alpha_factory.py` | Feature generation engine — contains the resample bug |
| `src/live_execution/live_trader.py` | Live trading loop, `build_live_features()` function |
| `src/live_execution/strategies/configurable_strategy.py` | Strategy with `_run_inference()` and sigmoid |
| `src/live_execution/data_manager.py` | Cache management, Panama Canal rollover |
| `src/data_processing/data_processor.py` | Training data processing pipeline |
| `src/LGBMLearner.py` | Model wrapper — `model.predict()` returns logits for focal loss |
| `models/registry/EXP-033_.../final_model.pkl` | LONG model (joblib, contains model + feature_names + params) |
| `models/registry/EXP-032_.../final_model.pkl` | SHORT model |
| `C:\CL_Analyst_Data\data\live_telemetry_cid18.db` | Live telemetry SQLite DB |
| `C:\CL_Analyst_Data\data\processed\warm_start_cache.parquet` | Current live cache (35K bars) |

## Expected Output

Write a report to `INVESTIGATION_RESULTS.md` in the project root with:

1. **Complete list of train-serve skew sources found** (with severity: critical/moderate/minor)
2. **Feature drift analysis** — which features drift most between train and live
3. **NaN handling differences** — exact comparison of train vs live fill behavior
4. **Mar 17 vs Mar 20 explanation** — whether the signal drop is data-driven or market-driven
5. **Recommended fixes** — ordered list of everything the Fix+Retrain agent should address, with code locations and specific instructions

Do NOT make any code changes. This is investigation only.
