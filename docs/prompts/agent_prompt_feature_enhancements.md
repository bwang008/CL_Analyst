# CL_Analyst Feature Engineering & Training Diagnostics Enhancement

## Objective

Enhance the CL_Analyst ML pipeline with (A) new features to improve model predictive power for CL crude oil futures, and (B) training diagnostics to better understand model convergence. All changes must maintain parity between the offline training pipeline and the live inference pipeline.

---

## Codebase Context

### Architecture Overview
- **Feature generation**: `src/features/alpha_factory.py` — `AlphaFactory` class with clustered feature methods
- **Data processing**: `src/data_processor.py` — `DataProcessor` class with versioned `process_set_*()` methods
- **Model training**: `src/LGBMLearner.py` — LightGBM wrapper with `add_evidence()` / `query()` interface
- **Walk-forward validation**: `src/walk_forward.py` — `WalkForwardSplitter` + `walk_forward_validate()`
- **Training orchestration**: `main.py` — `train_and_evaluate()` function
- **Live inference**: `src/live_execution/live_trader.py` — must compute same features at prediction time
- **Utilities**: `src/util.py` — `get_feature_columns()` is the single source of truth for what the model sees
- **Tests**: `tests/test_alpha_factory.py`, `tests/test_live_features.py`, `tests/test_pipeline_parity.py`

### Current Feature Set (~81 features)
Features are generated across 4 bar-level windows: `[864, 2016, 4032, 10080]` = `[3d, 7d, 14d, 35d]` in 5-min bars.

| Cluster | Features per window | Method |
|---------|-------------------|--------|
| **Volatility** | `VOL_PARK`, `VOL_RS`, `VOL_YZ`, `VOL_ROC`, `VOL_VOLVOL` (5) | `add_volatility_cluster()` |
| **Liquidity** | `LIQ_AMIHUD`, `LIQ_CORWIN` (2) | `add_liquidity_cluster()` |
| **Structure** | `STRUC_EFFICIENCY` (1 per window) + `STRUC_HURST_100`, `STRUC_ENTROPY_100` (once) | `add_structure_cluster()` |
| **Trend** | `TREND_DONCHIAN_POS`, `TREND_LR_SLOPE`, `TREND_LR_R2` (3) | `add_trend_cluster()` |
| **Volume Flow** | `VOLFLOW_OBV_SLOPE`, `VOLFLOW_DIVERGENCE`, `VOLFLOW_VWAP_DIST` (3) | `add_volume_flow_cluster()` |
| **Microstructure** | `STRUC_BODY_RATIO`, `STRUC_WICK_UP_RATIO`, `STRUC_WICK_LOW_RATIO`, `STRUC_COLOR` (4, once) | `add_microstructure_cluster()` |
| **Momentum** | `MOM_RSI_14`, `MOM_BB_Width`, `MOM_BB_PctB`, `MOM_ADX_14`, `MOM_DMP_14`, `MOM_DMN_14`, `MOM_MACD`, `MOM_MACD_Signal`, `MOM_MACD_Hist` (9, once) | `add_momentum_cluster()` |
| **Macro** | `MACRO_POS_1M`, `MACRO_WIDTH_1M`, `MACRO_POS_3M`, `MACRO_WIDTH_3M` (4, hourly-resampled) | `add_macro_context()` |
| **Time** | `Time_Sin`, `Time_Cos` (2) | Added in `data_processor.py` |
| **Other** | `log_ret`, `Volume_Log`, `ATR_14` (3) | Various |

### Feature Importance (what's working best)
Top features by mean importance (split count averaged across walk-forward folds):

**Long model**: `MACRO_POS_1M` (629) >> `TREND_DONCHIAN_POS_10080` (601) >> `MACRO_POS_3M` (60) > `Time_Sin` (56) > `TREND_DONCHIAN_POS_4032` (56)

**Short model**: `MACRO_POS_1M` (1098) >> `TREND_DONCHIAN_POS_10080` (869) >> `VOLFLOW_VWAP_DIST_10080` (245) > `TREND_DONCHIAN_POS_4032` (241) > `MACRO_POS_3M` (233)

**Lowest importance** (near-zero): `STRUC_COLOR`, `STRUC_BODY_RATIO`, `STRUC_WICK_UP_RATIO`, `STRUC_WICK_LOW_RATIO`

### LightGBM Model Parameters (current)
```json
{
  "num_leaves": 31, "min_child_samples": 166,
  "learning_rate": 0.0524, "feature_fraction": 0.694,
  "bagging_fraction": 0.648, "bagging_freq": 1,
  "reg_alpha": 2.737, "reg_lambda": 7.379,
  "max_depth": 4, "min_gain_to_split": 0.99,
  "n_estimators": 1000, "objective": "binary",
  "use_focal": true
}
```

---

## Part A: Feature Engineering Enhancements

### Task A1: Expand Macro Windows (HIGH PRIORITY)

**Rationale**: `MACRO_POS_1M` dominates feature importance. Adding shorter macro windows gives the model finer-grained regime context at the hourly-resampled level.

**File**: `src/features/alpha_factory.py`, method `add_macro_context()` (line 354)

**Change**: Update the default `macro_windows` dict:
```python
# BEFORE
macro_windows = {"1M": 840, "3M": 2160}

# AFTER
macro_windows = {"1D": 24, "3D": 72, "1W": 168, "2W": 336, "1M": 840, "3M": 2160}
```

**New features**: `MACRO_POS_1D`, `MACRO_WIDTH_1D`, `MACRO_POS_3D`, `MACRO_WIDTH_3D`, `MACRO_POS_1W`, `MACRO_WIDTH_1W`, `MACRO_POS_2W`, `MACRO_WIDTH_2W` (8 new)

---

### Task A2: Add 1-Day Bar-Level Window (HIGH PRIORITY)

**Rationale**: Current shortest bar-level window is 3 days (864 bars). Adding a 1-day window (288 bars) gives all clusters shorter-term resolution.

**File**: `src/data_processor.py`, all `process_set_*()` methods where `windows` is defined

**Change**: Add `1 * self.BARS_PER_DAY` (288) to the windows list:
```python
windows = [
    1 * self.BARS_PER_DAY,   # 288 = 1 day (NEW)
    3 * self.BARS_PER_DAY,   # 864 = 3 days
    7 * self.BARS_PER_DAY,   # 2016 = 7 days
    14 * self.BARS_PER_DAY,  # 4032 = 14 days
    35 * self.BARS_PER_DAY,  # 10080 = 35 days
]
```

**New features**: ~14 new `_288` variants across all window-dependent clusters

---

### Task A3: Return Distribution Features (MEDIUM PRIORITY)

**Rationale**: The model currently has no features describing the *shape* of the return distribution. Rolling skewness and kurtosis capture asymmetry and tail risk, which are highly predictive of upcoming volatility and trend reversals in commodities.

**File**: `src/features/alpha_factory.py`, new method `add_return_distribution_cluster()`

**Implementation**:
```python
def add_return_distribution_cluster(self, window: int) -> pd.DataFrame:
    """Rolling return distribution shape features."""
    suffix = f"_{window}"
    log_ret = self.df["log_ret"]

    # Rolling skewness: asymmetry indicator
    # Negative skew = fat left tail = crash risk
    self.df[f"DIST_SKEW{suffix}"] = log_ret.rolling(window).skew()

    # Rolling kurtosis: tail thickness
    # High kurtosis = extreme events likely
    self.df[f"DIST_KURT{suffix}"] = log_ret.rolling(window).kurt()

    # Rolling Z-score of current return vs recent distribution
    # How extreme is the current price move?
    roll_mean = log_ret.rolling(window).mean()
    roll_std = log_ret.rolling(window).std()
    self.df[f"DIST_ZSCORE{suffix}"] = (log_ret - roll_mean) / roll_std.replace(0, np.nan)

    return self.df
```

**Integration**: Call `self.add_return_distribution_cluster(window=window)` in the `add_all_features()` loop alongside other clusters.

**New features**: 3 per window × 5 windows = **15 new features**

---

### Task A4: Stochastic Oscillator (MEDIUM PRIORITY)

**Rationale**: Currently no mean-reversion oscillator except RSI. The Stochastic oscillator measures where the close is relative to the high-low range, and at multiple timeframes it captures short-term exhaustion that RSI may miss.

**File**: `src/features/alpha_factory.py`, new method `add_stochastic_cluster()`

**Implementation**:
```python
def add_stochastic_cluster(self, window: int) -> pd.DataFrame:
    """Stochastic oscillator features at multiple timeframes."""
    suffix = f"_{window}"
    
    roll_low = self.low.rolling(window).min()
    roll_high = self.high.rolling(window).max()
    range_span = (roll_high - roll_low).replace(0, np.nan)
    
    # %K: raw stochastic (same concept as Donchian pos but uses Close vs H/L range)
    stoch_k = (self.close - roll_low) / range_span
    self.df[f"MOM_STOCH_K{suffix}"] = stoch_k
    
    # %D: smoothed stochastic (3-bar SMA of %K)
    self.df[f"MOM_STOCH_D{suffix}"] = stoch_k.rolling(3).mean()
    
    return self.df
```

**Note**: This is related to `TREND_DONCHIAN_POS` but uses the High-Low range rather than Close-Close range, making it more sensitive to intrabar extremes.

**New features**: 2 per window × 5 windows = **10 new features**

---

### Task A5: Chaikin Money Flow (MEDIUM PRIORITY)

**Rationale**: Currently volume flow only uses OBV slope and VWAP distance. Chaikin Money Flow adds a volume-weighted close-location-value metric that measures buying/selling pressure directly, which ranked as a top feature in commodity futures ML research.

**File**: `src/features/alpha_factory.py`, extend `add_volume_flow_cluster()`

**Implementation** (add to existing `add_volume_flow_cluster()` method):
```python
# Chaikin Money Flow: volume-weighted close position
clv = ((self.close - self.low) - (self.high - self.close)) / (self.high - self.low).replace(0, np.nan)
mf_volume = clv * self.volume
self.df[f"VOLFLOW_CMF{suffix}"] = mf_volume.rolling(window).sum() / self.volume.rolling(window).sum().replace(0, np.nan)
```

**New features**: 1 per window × 5 windows = **5 new features**

---

### Task A6: Cross-Timeframe Ratios (MEDIUM PRIORITY)

**Rationale**: The model currently sees each timeframe in isolation. Cross-timeframe ratios (e.g., short-term volatility / long-term volatility) explicitly encode regime transitions — convergence vs divergence across scales. Research shows these reduce false signals.

**File**: `src/features/alpha_factory.py`, new method `add_cross_timeframe_ratios()`

**Implementation** (call after all windows are computed in `add_all_features()`):
```python
def add_cross_timeframe_ratios(self) -> pd.DataFrame:
    """Ratios between short and long-term features for regime detection."""
    # Volatility regime: short-term vol vs long-term vol
    if "VOL_PARK_288" in self.df.columns and "VOL_PARK_10080" in self.df.columns:
        self.df["CROSS_VOL_RATIO_1D_35D"] = (
            self.df["VOL_PARK_288"] / self.df["VOL_PARK_10080"].replace(0, np.nan)
        )
    if "VOL_PARK_864" in self.df.columns and "VOL_PARK_4032" in self.df.columns:
        self.df["CROSS_VOL_RATIO_3D_14D"] = (
            self.df["VOL_PARK_864"] / self.df["VOL_PARK_4032"].replace(0, np.nan)
        )

    # Trend regime: short-term Donchian vs long-term Donchian
    if "TREND_DONCHIAN_POS_288" in self.df.columns and "TREND_DONCHIAN_POS_10080" in self.df.columns:
        self.df["CROSS_TREND_DIFF_1D_35D"] = (
            self.df["TREND_DONCHIAN_POS_288"] - self.df["TREND_DONCHIAN_POS_10080"]
        )
    if "TREND_DONCHIAN_POS_864" in self.df.columns and "TREND_DONCHIAN_POS_4032" in self.df.columns:
        self.df["CROSS_TREND_DIFF_3D_14D"] = (
            self.df["TREND_DONCHIAN_POS_864"] - self.df["TREND_DONCHIAN_POS_4032"]
        )

    # VWAP regime: short-term vs long-term VWAP distance
    if "VOLFLOW_VWAP_DIST_288" in self.df.columns and "VOLFLOW_VWAP_DIST_10080" in self.df.columns:
        self.df["CROSS_VWAP_DIFF_1D_35D"] = (
            self.df["VOLFLOW_VWAP_DIST_288"] - self.df["VOLFLOW_VWAP_DIST_10080"]
        )

    return self.df
```

**New features**: **~5 new features** (fixed set, not per-window)

---

### Task A7: Day-of-Week Encoding (LOW PRIORITY)

**Rationale**: Currently only time-of-day is encoded (`Time_Sin`, `Time_Cos`). CL crude has known weekly seasonality patterns (e.g., EIA inventory report on Wednesdays, lower liquidity Fridays). Day-of-week encoding lets the model learn these patterns.

**File**: `src/data_processor.py`, method `add_time_features()`

**Implementation**: Add after existing `Time_Sin`/`Time_Cos`:
```python
# Day of week (cyclical): 0=Monday ... 4=Friday
day_of_week = df.index.dayofweek
df['Time_DayOfWeek_Sin'] = np.sin(2 * np.pi * day_of_week / 5)
df['Time_DayOfWeek_Cos'] = np.cos(2 * np.pi * day_of_week / 5)
```

**New features**: **2 new features**

---

### Task A8: Update Live Trader Feature Parity

**File**: `src/live_execution/live_trader.py`

After all new features are added to `AlphaFactory`, ensure the live trader's `_ALPHA_WINDOWS` list and `add_all_features()` call match the training pipeline exactly. The live trader must compute every feature the model expects at inference time.

- Update `_ALPHA_WINDOWS` to include the new 288-bar window
- Update any hardcoded `macro_windows` to include the new shorter windows
- Run `tests/test_live_features.py` and `tests/test_pipeline_parity.py` to verify parity

---

### Task A9: Create New Dataset Version (set_07)

**File**: `src/data_processor.py`

Create `process_set_07()` that includes all new features. Follow the pattern of `process_set_06()` but with:
- Updated `windows` list (add 288)
- Updated `macro_windows` dict (add 1D, 3D, 1W, 2W)
- New feature clusters integrated via `add_all_features()`

Register in `DATASET_VERSIONS` dict and update the `process()` router.

---

## Part B: Training Diagnostics Enhancements

### Task B1: Early Stopping & Convergence Tracking (HIGH PRIORITY)

**File**: `src/LGBMLearner.py`, method `add_evidence()`

**Change**: Add validation set monitoring and early stopping:

1. Reserve last 10% of training data as internal validation set
2. Pass `valid_sets` to `lgb.train()`
3. Add `early_stopping(stopping_rounds=50)` callback
4. Store `self.best_iteration_` and `self.evals_result_` on the model
5. If `best_iteration_ == n_estimators`, log a warning that more rounds may help

**Implementation sketch**:
```python
# In add_evidence(), after constructing train_data:
valid_frac = self.params.pop("validation_fraction", 0.1)
if valid_frac > 0 and n_rows > 100:
    split = int(n_rows * (1 - valid_frac))
    train_data = lgb.Dataset(X_mat[:split], label=y_arr[:split])
    valid_data = lgb.Dataset(X_mat[split:], label=y_arr[split:], reference=train_data)
    
    evals_result = {}
    self.model = lgb.train(
        lgb_params,
        train_data,
        num_boost_round=num_boost,
        valid_sets=[train_data, valid_data],
        valid_names=["train", "valid"],
        callbacks=[
            lgb.early_stopping(stopping_rounds=50, verbose=True),
            lgb.log_evaluation(period=100),
            lgb.record_evaluation(evals_result),
        ],
    )
    self.best_iteration_ = self.model.best_iteration
    self.evals_result_ = evals_result
else:
    # Fallback: train without validation
    self.model = lgb.train(lgb_params, train_data, num_boost_round=num_boost)
    self.best_iteration_ = num_boost
    self.evals_result_ = None
```

---

### Task B2: Per-Fold Training Diagnostics Logging (MEDIUM PRIORITY)

**File**: `src/walk_forward.py`, function `walk_forward_validate()`

**Change**: After training each fold, capture diagnostics:
```python
fold_result = {
    ...
    'best_iteration': getattr(model, 'best_iteration_', None),
    'training_log': getattr(model, 'evals_result_', None),
    'converged_early': getattr(model, 'best_iteration_', num_boost) < num_boost,
}
```

**File**: `main.py`, in the reporting section after walk-forward validation

**Change**: Aggregate and save training diagnostics:
- Save `training_diagnostics.json` with per-fold `best_iteration` and convergence status
- Print summary: "X/Y folds converged early, mean best iteration: Z"

---

## Verification Plan

### Automated Tests

1. **Run existing tests** to verify no regressions:
```bash
python -m pytest tests/test_alpha_factory.py -v
python -m pytest tests/test_live_features.py -v
python -m pytest tests/test_pipeline_parity.py -v
python -m pytest tests/ -v
```

2. **Update `tests/test_alpha_factory.py`**: Add test cases verifying:
   - New return distribution features produce non-NaN values on valid data
   - Stochastic K is bounded [0, 1]
   - CMF is bounded [-1, 1]
   - Cross-timeframe ratios exist when prerequisite columns are present
   - Day-of-week features are cyclical and bounded [-1, 1]

3. **Update `tests/test_live_features.py`**: Add all new feature names to the expected feature list

4. **Update `tests/test_pipeline_parity.py`**: Verify offline and live pipelines produce identical feature columns

5. **LGBMLearner convergence test**: Verify that `best_iteration_` is set after training and that `evals_result_` contains train/valid metrics

### Integration Verification

After all code changes:
1. Reprocess data: `python main.py process` (with set_07)
2. Run a smoke training: `python -m agent.experiment_runner` with a fast config
3. Verify the training diagnostics output (`training_diagnostics.json`) is saved
4. Compare new feature importance rankings vs baseline

---

## Priority Order for Implementation

| Priority | Task | Estimated New Features |
|----------|------|----------------------|
| 🔴 HIGH | A1: Expand macro windows | +8 |
| 🔴 HIGH | A2: Add 1-day bar-level window | +14 |
| 🔴 HIGH | B1: Early stopping & convergence | 0 (diagnostic) |
| 🟡 MED | A3: Return distribution (skew/kurt/zscore) | +15 |
| 🟡 MED | A5: Chaikin Money Flow | +5 |
| 🟡 MED | A6: Cross-timeframe ratios | +5 |
| 🟡 MED | A4: Stochastic oscillator | +10 |
| 🟡 MED | B2: Per-fold training diagnostics | 0 (diagnostic) |
| 🟢 LOW | A7: Day-of-week encoding | +2 |
| 🔴 HIGH | A8: Live trader parity update | 0 (parity) |
| 🔴 HIGH | A9: New dataset version (set_07) | 0 (integration) |

**Total new features**: ~59 (from ~81 to ~140)

---

## Important Constraints

1. **No lookahead bias**: All features must use only past data. No `.shift(-N)` in feature computation.
2. **Feature naming convention**: All feature columns follow the pattern `CLUSTER_NAME_window` (e.g., `DIST_SKEW_864`)
3. **Excluded prefixes**: Columns starting with `RAW_`, `TARGET_`, `META_` are automatically excluded from training by `get_feature_columns()` in `src/util.py`
4. **NaN handling**: `add_all_features()` replaces `inf` with `NaN` at the end. The `cleanup()` method handles remaining NaNs via ffill/bfill.
5. **Test parity**: `tests/test_pipeline_parity.py` enforces that the live pipeline produces the exact same features as the offline pipeline. This test MUST pass.
6. **warmup_rows**: The `cleanup()` method drops the first 10,500 rows. If adding very long windows, verify this is sufficient.
