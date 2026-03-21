# Train-Serve Feature Skew Investigation Results

**Date**: 2026-03-20  
**Scope**: Diagnostic analysis of feature mismatches between training-time and live-inference-time computation in CL_Analyst.  
**Models**: EXP-032 (SHORT, 154 features), EXP-033 (LONG, 154 features), both `set_08` + `use_focal=True`

---

## 1. Complete List of Train-Serve Skew Sources

### 🔴 Critical: MACRO Feature Resample Lookahead Bias

**Location**: [alpha_factory.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/features/alpha_factory.py#L389-L413) — `add_macro_context()`

```python
hourly = self.df.resample("1h").agg(ohlcv).dropna()
# Donchian on hourly...
macro = macro.reindex(self.df.index, method="ffill")
```

**The bug**: `resample("1h")` creates complete hourly bars using all 12 five-minute bars in that hour. The 10:00 hourly bar knows H/L through 10:55. `reindex(method="ffill")` assigns this value to bars starting at 10:05 — leaking up to 55 minutes of future data into training.

In live inference, the DataFrame ends at the current bar, so no future data exists — the current incomplete hour only has bars up to "now". This creates a systematic train-serve skew where training features saw subtly different MACRO values than live features at the same timestamps.

**Affected features** (12 total):
| Feature | Importance (EXP-032) | Importance (EXP-033) |
|---------|---------------------|---------------------|
| MACRO_POS_1M | **1032** (#1) | 306 |
| MACRO_POS_3M | **226** (#4) | 301 |
| MACRO_WIDTH_1M | 109 | 182 |
| MACRO_POS_1D | N/A | **959** (#6) |
| MACRO_POS_3D | N/A | **762** (#15) |
| MACRO_POS_1W | N/A | **591** (#29) |
| MACRO_POS_2W | N/A | 409 |
| MACRO_WIDTH_1D | N/A | 553 |
| MACRO_WIDTH_3D | N/A | 323 |
| MACRO_WIDTH_1W | N/A | 240 |
| MACRO_WIDTH_2W | N/A | 210 |
| MACRO_WIDTH_3M | 63 | 177 |

**Severity**: CRITICAL for EXP-032 — `MACRO_POS_1M` is the #1 most important feature (importance=1032, 30% more than #2). While the leak magnitude is small (~4% for 1D, <0.04% for 3M), the model's heavy reliance on these features means even small systematic bias in training creates skew.

**Proposed fix**: Replace `resample("1h")` with causally-safe bar-level rolling windows:
- `MACRO_1D`: Use `rolling(24 * 12)` directly on 5-min bars
- `MACRO_1M`: Use `rolling(840 * 12)` directly on 5-min bars  
- etc.

---

### 🟡 Moderate: NaN/Fill Handling Discrepancy

**Training pipeline** ([data_processor.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/data_processor.py#L534-L644) `cleanup()`):
1. Drops first **10,500 warmup rows** (ensures all rolling windows fully populated)
2. `ffill().bfill()` on non-target columns
3. `dropna()` — drops any rows still containing NaN
4. Result: **Model never sees zero-filled or NaN features**

**Live pipeline** ([live_trader.py](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/live_execution/live_trader.py#L293-L301) `build_live_features()`):
1. `replace([inf, -inf], NaN)`
2. `ffill()` → `bfill()` → `fillna(0)`
3. No warmup row dropping
4. Result: **Cold-start features get filled with 0**, a value the model never saw during training

**Impact**: During normal operation with a warm cache (~35K bars), this is **low impact** — the cache has enough history for all rolling windows, so `fillna(0)` is rarely invoked. However, during cold start or after cache corruption, features like `VOL_ROC_10080` (needs 20,160 bars) or `MACRO_3M` (needs ~25,920 bars) could get zero-filled, producing nonsensical model inputs.

> [!IMPORTANT]
> This skew existed before the rollover fix and does NOT explain the signal suppression, since the cache has 35K bars (sufficient for all windows).

**Proposed fix**: 
- In `build_live_features()`, after computing features, check if any feature has a suspicious 0 value that should never be 0 (e.g., volatility features) and log a warning
- Consider matching the training behavior: drop the last row if it has NaN features rather than zero-filling

---

### 🟢 Minor: `log_ret` Column — Training Keeps It, Live Recomputes It

**Training**: `AlphaFactory.__init__()` creates `log_ret = log(close/close.shift(1))`. The first row is NaN (no prior close). `cleanup()` drops this via the warmup window.

**Live**: Same computation, but `ffill()` fills the first NaN with some value (likely 0 from `fillna(0)`). Since `log_ret` has very low feature importance (EXP-032: 27, EXP-033: 243), this is negligible.

---

## 2. Feature Drift Analysis: Training vs Live Telemetry

Compared 1,233 live telemetry records (Mar 13–20, 2026) against 1.2M training records.

### Summary

| Model | Drifted Features | Non-Drifted | Total |
|-------|-----------------|-------------|-------|
| EXP-032 (SHORT) | **50** | 104 | 154 |
| EXP-033 (LONG) | **50** | 104 | 154 |

### Key Drifted Features (Both Models)

The dominant drift pattern is **elevated volatility** — the live period shows significantly higher vol than the training distribution average:

| Feature Group | Example | Live z-score | Explanation |
|--------------|---------|-------------|-------------|
| **VOL_ROC** (all windows) | VOL_ROC_4032: z=+9.66 | +1.9 to +9.7 | Volatility is rising faster than historical norm |
| **VOL_VOLVOL** (all windows) | VOL_VOLVOL_4032: z=+7.53 | +3.2 to +7.5 | Vol-of-vol extremely elevated |
| **VOL_PARK/RS/YZ** (long windows) | VOL_RS_2016: z=+4.04 | +1.7 to +4.1 | Absolute volatility above 95th pctl |
| **TREND_LR_SLOPE** (long windows) | SLOPE_4032: z=+3.76 | +2.5 to +3.8 | Strong uptrend in longer windows |
| **MACRO_WIDTH** (1W, 2W) | WIDTH_2W: z=+3.43 | +3.0 to +3.4 | Broader price ranges than training norm |
| **LIQ_CORWIN** (long windows) | CORWIN_2016: z=+3.83 | +1.5 to +3.8 | Wider bid-ask spreads |
| **Volume_Log** | z=-1.84 | -1.84 | Lower volume than training average |
| **MOM_DMP/DMN_14** | DMP: z=+2.79 | +2.6 to +2.8 | Elevated directional movement |

### Critical Non-Drifted Feature: MACRO_POS_1M ✅

The #1 most important feature for EXP-032 (`MACRO_POS_1M`, importance=1032) is **NOT drifted** (z=+0.23, live_mean=0.60, well within training [5th, 95th] range). This indicates the model's primary input is receiving valid, in-distribution data.

### Drift Assessment

The drift pattern is consistent with a **normal market regime shift** (elevated volatility period), not a data pipeline bug. The most important features for both models remain within normal ranges. The drifted features are mostly volatility-derived and reflect the current market environment.

---

## 3. NaN Handling Comparison: Training vs Live

| Aspect | Training | Live |
|--------|----------|------|
| **Inf handling** | `AlphaFactory.add_all_features()` calls `replace([inf, -inf], NaN)` at line 243 | `build_live_features()` calls same at line 298 | 
| **Warmup** | Drops first 10,500 rows | No rows dropped — relies on cache depth |
| **Fill strategy** | `ffill().bfill()` then `dropna()` | `ffill() → bfill() → fillna(0)` |
| **Zero fill** | **Never** (dropna removes any remaining NaN) | **Yes** — last resort for all-NaN features |
| **Model exposure** | Model trained on data with NO zeros from fill | Model may see zeros from `fillna(0)` in live |

**Concrete risk**: If cache has insufficient history (< ~25,920 bars for MACRO_3M), features like `EXHAUST_CUM_ATR_10080` (exhaust cumulative ATR over 35 days) would be all-NaN → filled with 0. The model never saw 0 for these features during training.

**Current status**: Cache has 35,275 bars — sufficient for all features. This is NOT currently causing issues but is a latent bug.

---

## 4. Mar 17 vs Mar 20 Deep Dive

### Context
- **Mar 17 20:20**: Model produced `sell=0.99` (strong SHORT signal)
- **Mar 20 19:20**: Model produced `sell=0.48` (below threshold, no signal)

### Feature Changes

**99 of 154 features changed >10%** between these two timestamps.  
**39 features changed >100%.**

### Top Features Driving SHORT Model (EXP-032) Signal Suppression

| Feature | Mar 17 | Mar 20 | Change | Importance | Impact |
|---------|--------|--------|--------|-----------|--------|
| VOLFLOW_VWAP_DIST_864 | -0.0012 | +0.0218 | **+1913%** | 183 | 3506 |
| TREND_LR_R2_2016 | 0.016 | 0.145 | **+784%** | 172 | 1352 |
| STRUC_EFFICIENCY_864 | 0.004 | 0.021 | **+432%** | 82 | 356 |
| TREND_LR_SLOPE_864 | -0.0006 | +0.0007 | **+213%** | 149 | 317 |
| VOLFLOW_OBV_SLOPE_864 | -52.5 | +24.8 | **+147%** | 132 | 194 |
| VOL_ROC_2016 | +1.10 | -0.42 | **-138%** | 138 | 191 |
| TREND_DONCHIAN_POS_864 | 0.35 | 0.80 | **+130%** | 122 | 159 |
| TREND_DONCHIAN_POS_2016 | 0.45 | 0.81 | **+78%** | 169 | 132 |

### Interpretation

The feature changes tell a clear **market regime story**:

1. **Trend flipped bullish**: TREND_LR_SLOPE_864 went from -0.0006 (slightly bearish) to +0.0007 (slightly bullish). TREND_DONCHIAN_POS at all windows moved toward 1.0 (near channel highs).

2. **Price above VWAP**: VOLFLOW_VWAP_DIST_864 went from -0.001 (below VWAP) to +0.022 (above VWAP) — the market moved from a bearish stance to bullish.

3. **OBV slope flipped positive**: VOLFLOW_OBV_SLOPE_864 went from -52.5 (heavy selling) to +24.8 (buying pressure) — complete reversal of volume flow.

4. **MACRO features barely changed**: MACRO_POS_1M (the #1 feature) only moved +5% (0.61→0.64). MACRO_POS_3M moved +5.2%. These longer-term features show the broader trend hasn't changed much — but the model's shorter-term features now look bullish.

### Conclusion

> **The signal drop from sell=0.99 to sell=0.48 is MARKET-DRIVEN, not data-driven.** The model is correctly responding to a genuine bullish reversal in short-term momentum, trend, and volume features. The SHORT model requires bearish conditions to produce sell signals, and the market shifted bullish between Mar 17 and Mar 20.

The model is behaving as designed — it won't produce short signals in a bullish environment. This is not "signal suppression" caused by a bug; it is the model correctly identifying that short conditions are not present.

---

## 5. Recommended Fixes for the Fix+Retrain Agent

### Fix 1: MACRO Resample Lookahead (CRITICAL)

**Location**: [alpha_factory.py:389-413](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/features/alpha_factory.py#L389-L413)

**Action**: Replace hourly resample with bar-level rolling windows:

```python
# BEFORE (lookahead bug):
hourly = self.df.resample("1h").agg(ohlcv).dropna()
# Donchian on hourly...
macro = macro.reindex(self.df.index, method="ffill")

# AFTER (causally safe):
for label, hours in macro_windows.items():
    bars = hours * 12  # Convert hours to 5-min bars
    roll_max = self.high.rolling(bars).max()
    roll_min = self.low.rolling(bars).min()
    range_span = roll_max - roll_min
    self.df[f"MACRO_WIDTH_{label}"] = range_span / self.close
    self.df[f"MACRO_POS_{label}"] = (self.close - roll_min) / range_span
```

**After fix**: Retrain both models since MACRO features will have different distributions.

### Fix 2: NaN Fill Harmonization (MODERATE)

**Location**: [live_trader.py:293-301](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/live_execution/live_trader.py#L293-L301)

**Action**: Add a safety check after `fillna(0)`:
```python
# After fillna(0), log features that were zero-filled
zero_filled = (work.iloc[-1] == 0) & work.iloc[-2].notna() & (work.iloc[-2] != 0)
if zero_filled.any():
    log.warning("Zero-filled features (cold start): %s", 
                zero_filled[zero_filled].index.tolist())
```

Alternatively, match training behavior exactly: if any feature in the last row is NaN after ffill/bfill, return `None` from `build_live_features()` to skip that bar.

### Fix 3: Cache Depth Validation (MINOR)

**Location**: [live_trader.py:245-250](file:///c:/Users/bwang/Documents/GitHub/CL_Analyst/src/live_execution/live_trader.py#L245-L250)

**Action**: Add a check for minimum recommended cache depth:
```python
MIN_RECOMMENDED_BARS = 26_000  # Enough for MACRO_3M + warmup
if len(df) < MIN_RECOMMENDED_BARS:
    log.warning("Cache depth %d below recommended %d — "
                "long-window features may be unreliable",
                len(df), MIN_RECOMMENDED_BARS)
```

### Fix 4: No Additional Lookahead Issues Found ✅

The audit of `alpha_factory.py` confirmed:
- No `resample()` calls besides the known MACRO bug
- No `shift(-N)` (forward-looking shift) anywhere
- No `rolling(center=True)` anywhere
- `pandas_ta` functions (RSI, ADX, MACD, BBands, ATR, OBV, linreg) are all industry-standard implementations that use only past data — no lookahead
- All rolling windows use `min_periods` correctly

---

## Summary

| Issue | Severity | Signal Suppression Cause? | Fix Required? |
|-------|----------|--------------------------|---------------|
| MACRO resample lookahead | 🔴 Critical | Contributes to train-serve skew but was present when signals were working | **Yes** — before retraining |
| NaN/fillna(0) mismatch | 🟡 Moderate | No (cache is deep enough) | Yes — defensive fix |
| Market regime shift (Mar 17→20) | ℹ️ Explanation | **Yes** — the primary cause of signal drop | No — model is correct |
| Additional lookahead | 🟢 None found | N/A | N/A |

> **Bottom line**: The sell signal dropping from 0.99 to 0.48 is a **correct model response** to a bullish market shift, not a pipeline bug. The MACRO resample bug should be fixed before retraining to improve train/live parity, but it alone did not cause the signal suppression. The model's design (SHORT signals require bearish conditions) is working as intended.
