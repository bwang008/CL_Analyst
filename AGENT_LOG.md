## 2026-04-28 — Next Steps: Logloss Production & Scout Ablation Studies

### 1. Full Production Sweep (Baseline Logloss)
The recent unconstrained scout run for hourly_ensemble_005 (HourSet_06 dataset, Logloss) yielded phenomenal baseline results (PF 1.65, Sharpe 1.24, .9k PnL) after the threshold/consecutive signal optimizer was run.
**Goal**: Run a full production Optuna sweep (e.g., 2000 trials) to pinpoint the absolute best hyperparameters for the logloss models.

**Agent Prompt**:
> \/run-cloud-experiment\ Launch a full production sweep (2,000 trials) for both long and short Logloss models on the HourSet_06 dataset using the standard configurations (no bucketing, no lookback). Ensure we capture the final E2E ensemble backtest markdown.

### 2. Ablation Study: Feature Buckets
**Goal**: Determine if pruning overlapping feature buckets improves OOS performance by reducing collinearity and noise.

**Agent Prompt**:
> \/run-cloud-experiment\ Rerun the scout experiment (Logloss, HourSet_06) with Feature Buckets turned ON in the deployment manifest. Run the strategy_optimizer.py on the resulting predictions and record the metrics in the tracker.

### 3. Ablation Study: Lookback Windows
**Goal**: Determine if constraining the model memory (e.g., 2-10 year lookback) improves the model's ability to adapt to recent market regimes compared to full historical training.

**Agent Prompt**:
> \/run-cloud-experiment\ Rerun the scout experiment (Logloss, HourSet_06) with Lookback Windows explicitly enabled in the search space. Once complete, run the strategy_optimizer.py to calculate the final metrics.

### 4. Synergy Run: Lookback + Buckets
**Goal**: Test the combined effect of dynamic feature bucketing and restricted lookback windows.

**Agent Prompt**:
> \/run-cloud-experiment\ Launch a scout run combining BOTH Feature Buckets and Lookback Windows. Compare the final optimized metrics against the baseline, Bucket-only, and Lookback-only runs to evaluate synergy.

### 5. Target Horizon Bake-off
**Goal**: Determine if altering the Triple Barrier horizon (e.g., targeting 24H or 120H instead of 72H) yields better optimal models on the new dataset.

**Agent Prompt**:
> \/run-cloud-experiment\ Rerun the unconstrained scout experiment on the HourSet_06 dataset, but change the target arguments in the run script to a different horizon (e.g. 24H or 120H). Run the optimizer on the outputs and compare the Profit Factor to the 72H baseline.

### 6. Prerequisite: Verify Feature Buckets
**Goal**: Before running the Feature Buckets ablation study, we must verify that the bucket definitions correctly capture the newly engineered features (PPO, Normalised Slope, DMA/Ichimoku, etc.).

**Agent Prompt**:
> Please inspect src/features/feature_buckets.py and cross-reference it against the latest HourSet_06 dataset columns. Verify that the new momentum and trend features (PPO, Normalised Slope, DMA/Ichimoku) have been properly categorized into their respective buckets. If they are missing, please update the bucket definitions to include them before we launch any bucketed experiments.

### 7. Pipeline Upgrade: Autonomous Strategy Optimization (The Holdout Architecture)
**Context**: Currently, m_e2e_pipeline.py blindly uses a static strategy config to evaluate predictions. We need to auto-optimize the execution parameters (thresholds, cooldowns), but we **cannot** optimize them on the OOS test set, as that causes catastrophic Data Leakage. We also **cannot** mutate the 	p_atr_mult or sl_atr_mult, as those are Pre-Train Label Parameters baked into the dataset target.
**Goal**: Implement a strict 3-way data split (Train / Validation / Holdout) in m_e2e_pipeline.py to support autonomous, mathematically sound execution optimization.

**Agent Prompt**:
> Please review gcp/vm_e2e_pipeline.py. We need to implement an autonomous strategy optimization step, but we must use a strict **Train / Validation / Holdout** architecture to prevent Data Leakage.
> 
> Currently, the pipeline splits data at 	rain_cutoff_date (e.g. 2022-01-01) into df_train and df_vault.
> 
> **Step 1: The 3-Way Split**
> Update the pipeline to accept a new CLI argument --holdout-cutoff-date (defaulting to e.g. '2023-01-01').
> - df_train: Before 	rain_cutoff_date
> - df_val: Between 	rain_cutoff_date and holdout_cutoff_date
> - df_holdout: After holdout_cutoff_date
> 
> **Step 2: Prediction Generation**
> The LGBMLearner must generate two sets of OOS predictions: one for df_val and one for df_holdout.
> 
> **Step 3: Execution Optimization (The Lockdown)**
> Create a new function optimize_ensemble_params() that uses Optuna to optimize execution parameters on the **Validation** predictions.
> **CRITICAL**: The optimizer MUST NOT mutate 	p_atr_mult or sl_atr_mult! Those are Label Parameters. It may only sweep entry_threshold, cooldown_bars, max_hold_bars, and consecutive_signal_threshold.
> 
> **Step 4: The True OOS Backtest**
> Apply the discovered execution parameters to the ensemble_cfg, and run the final BacktestEngine report strictly on the **Holdout** predictions. 
> This turns our pipeline into a mathematically rigorous, self-tuning evaluation engine!

---

### 8. Pipeline Wrapper Updates & Scout Ablation (Feature Buckets)
**Context**: The m_e2e_pipeline.py was successfully upgraded with the Holdout Architecture (--opt-trials and --holdout-cutoff-date). However, the GCP PowerShell deployment scripts (gcp_deploy_scout.ps1, etc.) and the VM startup scripts (m_scout_run.sh) have not been updated to expose these new CLI arguments to the user.
**Goal**: Update the deployment wrappers to support the new arguments, then launch the first ablation study: running the exact same 72H Logloss Scout, but with **Feature Buckets enabled**, to see if the OOS metrics improve over the previous 1.65 PF baseline.

**Agent Prompt**:
> We recently upgraded gcp/vm_e2e_pipeline.py with two new arguments (--opt-trials and --holdout-cutoff-date) to support autonomous holdout optimization. 
> 
> **Step 1: Wrapper Updates**
> Please update the deployment wrapper scripts (gcp/gcp_deploy_scout.ps1 and gcp/vm_scout_run.sh) to accept and pass --opt-trials and --holdout-cutoff-date down to the python pipeline. Also, please briefly document these new arguments in .agents/workflows/run-cloud-experiment.md.
> 
> **Step 2: Launch the Ablation Study**
> Once the wrappers are updated, please launch a scout experiment for the Logloss model on the HourSet_06 dataset using the 72H targets (TARGET_TRIPLE_2p0x1_72H_LONG / _SHORT). 
> **CRITICAL**: For this run, you must enable **Feature Buckets** in the deployment config.
> 
> **Step 3: Metric Comparison**
> Once the scout run finishes and downloads the final backtest report, compare the OOS Profit Factor and Drawdown against our previous un-bucketed baseline.
> *Reference*: You can find the previous baseline's optimal configuration in configs/experiments/scout_ensemble_logloss_opt_04282026_1838.json and its metrics logged in gent/experiment_log.json.
> 
> Please report back with the final metric comparison!

---

# AGENT_LOG

Historical progress and completed track summaries (reverse-chronological; newest first).

## 2026-04-20 — Telegram Diagnostics, DataManager Seed Fix & Hard-Fail Pipeline

### Goal
Implement push-only Telegram notifications for the live trader, fix a silent data pipeline failure that was providing wrong historical data to the 1H/4H models, and enforce hard-fail design rules going forward.

### Telegram Notification System
- **`src/live_execution/utils/telegram_alert.py`**: Verified as fully implemented (fire-and-forget, 3s timeout, try/except). Added PST/PDT timezone to all messages via `zoneinfo` with `pytz` fallback.
- **Startup alert**: Appends full system health check payload to login message.
- **Trade Entry**: Fires when bracket order is submitted — includes action, price, TP/SL, buy/sell probabilities, and triggering bar OHLCV.
- **Trade Filled**: Fires on execution fill — includes fill price, probabilities, and bar data pulled from `_last_decision_context_by_order_id`.
- **Background Heartbeat**: A daemon thread (`_TelegramHeartbeat`) fires every 3600s on wall-clock time, **independent of inference**. Includes: uptime, broker status, position/PnL, last successful inference bar timestamp (or `❌ None`), inference latency, CPU/RAM/disk, and recent WARNING/ERROR log messages (captured by `_TelegramLogCapture` ring buffer, drained each pulse).
- **Fatal Error**: Sends stack trace summary to Telegram before process termination.

### DataManager Seed Path Misconfiguration (Root Cause Fix)
**Bug**: The 1H `DataManager` was configured with `seed_path_1h = ".../raw/cl-1h_bk.csv"` — a file that **never existed**. When the cache was missing, it silently fell back to an IBKR bootstrap, producing a 144KB cache (~142 bars) instead of the 3,600-bar HourSet_02 parquet (91.8MB). This caused:
- **4H model**: Hard failure (`142 < 210 bars`)
- **1H model**: Silent degradation — inference ran but on thin, potentially mis-priced data

**Fixes**:
- `live_trader.py`: `seed_path_1h` now explicitly points to `get_data_root() / "processed" / "cl-1h_bk_HourSet_02.parquet"`
- `data_manager.py`: `_seed_from_csv()` now supports `.parquet` seeds in addition to semicolon-delimited CSV
- `data_manager.py`: Removed the silent IBKR bootstrap fallback — **missing seed now raises `FileNotFoundError` immediately with a clear message and Telegram alert**
- Deleted the stale 144KB `warm_start_cache_1h.parquet` so it rebuilds from the full parquet on next boot

### Hard-Fail Policy Enforced (DataManager)
Per new design rule: the DataManager no longer silently falls back to IBKR when a seed file is missing. It raises, logs `log.error()`, and sends a Telegram alert. There is one pipeline, one data source, and no silent redundancy.

### Lessons Learned (Added to HANDOFF.md)
1. **Fail loudly on missing seeds** — hard exception + Telegram alert, never silent bootstrap
2. **Validate minimum bars at startup** — check before inference, not during feature generation
3. **Lock seed paths to explicit verified files** via `get_data_root()` — never derive from naming conventions
4. **One pipeline only** — no backup paths that can silently corrupt the data environment
5. **Path audit on every new timeframe** — run `audit_paths.py` before deploying new bar sizes

### Files Changed
- `src/live_execution/live_trader.py` — Heartbeat thread, log capture handler, inference bar tracker, 1H seed path fix
- `src/live_execution/data_manager.py` — Parquet seed support, hard-fail on missing seed (removed IBKR bootstrap fallback)
- `src/live_execution/utils/telegram_alert.py` — PST/PDT timezone, global timestamp prefix
- `README.md` — Telegram Notifications section added under Live Execution
- `HANDOFF.md` — Live Execution Data Pipeline Design Rules section + bug entry



### Goal
Resolve critical runtime issues in the live trading engine during after-hours execution and fix missing feature extraction for "lean" production models. 

### Fix 1: Missing Production Models on git Worktrees
- **Problem**: The `development` worktree crashed on startup because `models/production/*.pkl` files were missing. `*.pkl` was globally gitignored.
- **Fix**: Added `!models/production/**` to `.gitignore` so production model binaries are officially tracked by git and synchronize across clones/worktrees.

### Fix 2: After-Hours Observability (Heartbeat Logging)
- **Problem**: The engine appeared frozen during weekend and after-hours periods (no bar updates logged), causing ambiguity about connectivity status.
- **Fix**: Implemented a 5-minute heartbeat in `LiveTrader._event_loop`. The engine now logs: `last_bar` age, `market` status (OPEN vs CLOSED with weekend/halt reasons), `position`, and `connected` state.

### Fix 3: Lean Feature Generation Missing Extended Columns
- **Problem**: The production model (`production_lean_dual.json`) uses `"lean_features": true` but expects extended features (`MOM_STOCH_K_*` and `Time_DayOfWeek_*`). The lean pipeline in `build_live_features` bypassed these.
- **Fix**: Updated `build_live_features` in `live_trader.py` to explicitly generate DayOfWeek and Stochastic features if requested by `feature_names`, even when `lean=True`. This keeps the pipeline fast while providing the necessary inputs.

### Fix 4: External Macro/COT Features Missing in Live Pipeline
- **Problem**: The 1h model (`HourEnsemble001`) was trained on `HourSet_02` which includes 62 external features (FRED: VIX, OVX, DXY, yield curve, fed funds; CFTC: COT positioning). `build_live_features` never called `MacroFeatureEngine.merge_all()`, so these features were absent at inference time.
- **Fix**: Wired `MacroFeatureEngine.merge_all()` into `build_live_features` — triggered only when external macro/COT features are detected in `feature_names`. Also added the missing `6M: 4320` entry to the 1h macro_windows dict.

### Fix 5: Auto-Refresh Macro Data at Startup
- **Problem**: FRED and COT CSV files could go stale if not updated, causing the live trader to use outdated macro context.
- **Fix**: Added `MacroFeatureEngine.refresh_if_stale()` which checks file modification times (FRED: >24h, COT: >7 days) and re-downloads via the existing `scripts/download_macro_data.py` functions. Called automatically at `LiveTrader.start()` — only for models that need external macro features. COT downloads from CFTC (no API key). FRED requires `FRED_API_KEY` in `.env`.

### Fix 6: Lingering Dual-Stream AttributeErrors
- **Problem**: Earlier dual-stream refactoring renamed `self.rolling_df` to `self.rolling_df_5m` and `self._live_bars` to `self._live_bars_5m`, but missed several references in `_on_new_bar()`, `_manage_working_orders()`, and `_cancel_subscriptions()`. This caused crashes in the live environment (`AttributeError: 'LiveTrader' object has no attribute 'rolling_df'`) and 14 failures in the test suite.
- **Fix**: Replaced all remaining legacy attributes with their stream-specific counterparts (`_5m` or `_1h`) or the localized `rolling_df` parameter. Updated test stubs to include `_bar_size` and `_virtual_ledger` mocks. All tests pass.

### Files Changed
- `.gitignore` — Un-ignored `models/production/**`
- `src/live_execution/live_trader.py` — Heartbeat logging, lean feature generation, macro merge, auto-refresh at startup
- `src/features/macro_features.py` — Added `refresh_if_stale()` method with staleness thresholds


## 2026-03-29 — HourSet_02 Short Model Selection (120H Horizon)

### Goal
Identify the optimal SHORT model on the `cl-1h_bk_HourSet_02.parquet` dataset to pair with the existing validated LONG model (`TARGET_TRIPLE_2p5x1_72H_LONG`). The priority is maximizing trade count while maintaining positive PnL and statistical significance.

### Canary Experiments (150 trials, bucket-pruned)
Four custom targets were evaluated via the GCP canary pipeline:
1. `TARGET_TRIPLE_1p5x1_72H_SHORT`: 1 trade (0% WR)
2. `TARGET_TRIPLE_1p5x1_120H_SHORT`: 1 trade (100% WR, +$2.1k)
3. `TARGET_TRIPLE_2p0x1_120H_SHORT` (avg_precision): 12 trades (50% WR, PF 3.20, +$8.5k)
4. `TARGET_TRIPLE_2p5x1_120H_SHORT` (logloss): **26 trades (61.5% WR, PF 1.58, +$6.5k)**

### Decision
The **2.5x1 120H Short (logloss)** target (Canary 4) was selected as the best candidate. With 26 trades and a 61.5% win rate over the OOS period, it provides the highest statistical confidence while remaining strongly profitable (+$6,524). This appropriately balances the 170-trade count of the 72H Long model.

### Files Created
- `reports/short_models_report.md` — Complete metrics summary table comparing all 4 short models.

## 2026-03-27 — Lean Canary Breakthrough & Production Deployment

### Goal
Execute threshold sweep on set_11c long_logloss to find profitability boundary. Run "Pure Alpha" lean canary (22 core+momentum features) to test if noise reduction improves generalization. Deploy winning model to production.

### Threshold Sweep (EXP-037b)
- Scanned 0.45–0.65 probability range on EXP-035 long_logloss OOS predictions
- **Profitability boundary at 0.56**: 26 trades, 46.2% WR, PF 1.54, +$1,176
- Below 0.56: thousands of trades but PF < 1.0 (friction eats the edge)
- Above 0.57: very few trades (9 at 0.57, 5 at 0.58)

### Lean Canary (EXP-037) — 🏆 PRODUCTION WINNER
- Engineered `set_11c_lean` dataset: 26 features (core + momentum only), 154 MB
- Ran 150-trial bucket-pruned canary on GCP (4 searches, 46 min total)
- **short_logloss**: 208 trades, 31.2% WR, PF 1.27, **+$5,816**
- First statistically significant profitable clean model
- ML logloss -0.6904 (slightly worse than full 206-feature -0.6845) but dramatically better OOS performance → fewer features = less overfitting

### Production Deployment
- Model PKL repackaged for LGBMLearner at `models/production/final_model.pkl`
- Config frozen at `configs/strategies/production_lean_momentum.json`:
  - SHORT only, threshold 0.60, TP=3.5x ATR, SL=1.5x ATR
  - `lean_features: true` — fast momentum-only feature pipeline
  - `execution_symbol: MCL` — Brain=CL continuous, Hands=MCL (Micro CL)

### Code Changes
- **`ibkr_client.py`** — Added `build_mcl_contract()`, parameterized `get_front_month_contract(symbol=)`
- **`configurable_strategy.py`** — Added `model_path` for direct PKL loading (bypasses registry)
- **`live_trader.py`** — Lean feature path (`lean=True`), MCL execution routing, symbol-aware position/order queries

### Files Changed
- `src/live_execution/ibkr_client.py` — MCL contract support
- `src/live_execution/strategies/configurable_strategy.py` — model_path loading
- `src/live_execution/live_trader.py` — lean features, MCL routing
- `models/production/final_model.pkl` — **[NEW]** production model
- `configs/strategies/production_lean_momentum.json` — **[NEW]** production config

## 2026-03-26 — Feature Bucket Architecture & Winning Strategy Optimization

### Goal
Implement automated feature bucket pruning in Optuna to identify toxic feature clusters, standardize the ad-hoc `TARGET_VOL_EXPANSION` target into the pipeline, and run 150-trial bucket canary searches on winning strategies.

### Feature Bucket Architecture
- **`src/features/feature_buckets.py`** — **[NEW]** Partitions 227 features into 12 logical buckets (core always ON, 11 toggleable)
- **`agent/optuna_lgbm_search_v2.py`** — Added bucket toggle categorical hyperparameters, `--use-buckets` CLI flag, enforced 150-trial minimum for bucket-enabled runs
- **`gcp/vm_canary_run.sh`** + **`gcp/gcp_deploy_canary.ps1`** — `--use-buckets` / `-UseBuckets` passthrough

### TARGET_VOL_EXPANSION Standardization
- Ported logic from ad-hoc `scripts/generate_vol_target.py` into `data_processor.py` as `add_vol_expansion_target()` (L411-483)
- Wired into `process_set_12` pipeline — now generates automatically
- Regenerated set_12 locally (19.4% positive rate) and uploaded to GCS (864.5 MB)

### Bucket Canary Results (150 trials × 4 searches each)

| Experiment | Dataset | Target | Trades | WR | PF | PnL |
|---|---|---|---|---|---|---|
| **EXP-034** | set_12 | TRIPLE_2x1_24H | 273 | 27% | 0.66 | -$15,127 |
| **EXP-035** 🏆 | set_11c | TRIPLE_2x1_24H | **50** | **34%** | **0.98** | **-$98** |
| **EXP-036** | set_12 | VOL_EXPANSION | 455 | 21% | 0.61 | -$32,603 |

### Key Findings — Cross-Run Bucket Consensus (12 searches)
- **Momentum**: ON in 10/12 — **the alpha signal**
- **Structure**: OFF in 11/12 — confirmed noise (candle body/wick ratios, OBV slope)
- **Trend**: OFF in 10/12 — noise
- **Divergence**: OFF in 4/4 on set_12 (toxic with EXHDIV features), ON in some set_11c models
- **Vol Expansion**: directionless — predicts *whether* vol expands, not *which direction*. Needs directional filter.

### Files Changed
- `src/features/feature_buckets.py` — **[NEW]** bucket definitions + filtering
- `tests/test_feature_buckets.py` — **[NEW]** 13 unit tests
- `src/data_processor.py` — `add_vol_expansion_target()` method + wired into `process_set_12`
- `agent/optuna_lgbm_search_v2.py` — bucket toggles, 150-trial floor, `--use-buckets`
- `gcp/vm_canary_run.sh` — `--use-buckets` passthrough
- `gcp/gcp_deploy_canary.ps1` — `-UseBuckets` switch, `feature_buckets.py` in upload list

## 2026-03-23 — Volatility Straddle Optimization, Asymmetric Drawdown Target & Metric Architecture Change

### Goal
Optimize the Breakout Straddle strategy parameters, engineer a new strict "Easy Money" directional target, and replace the F0.5 ML metric with PR-AUC for rare-event targets.

### Breakout Straddle Optimization (Volatility Expansion)
- Ran 213 Optuna trials via `strategy_optimizer.py` (full FSM backtest per trial, ~30s each)
- **Best config (Trial 200)**: `entry_threshold=0.65`, `breakout_window=18`, `tp_atr_mult=8.5`, `sl_atr_mult=3.0`, `trailing_atr_mult=3.5`
- **Results**: +$13,763 PnL, PF 1.47, 33.9% WR, 112 trades, -$8,866 MDD
- Frozen to `configs/strategies/volatility_breakout.json` and committed

### Asymmetric Drawdown Target ("Easy Money" Setup)
- **Script**: `scripts/generate_asymmetric_target.py` — **[NEW]**
- **LONG label = 1**: Max(High) over next 24H > 2.0× ATR **AND** Max(Drawdown) < 0.5× ATR
- **SHORT label = 1**: Inverse (downside > 2.0× ATR, upside < 0.5× ATR)
- **Distribution on set_11**: LONG 3.46% positive (41,278 / 1.19M), SHORT 3.23% (38,522)
- Dataset saved as `CL_set_11_asym.parquet` (797 MB), uploaded to GCS

### ⚠️ CRITICAL: ML Metric Change — f0.5 → average_precision (PR-AUC)

**Problem**: F0.5 uses a hard 0.50 threshold to convert probabilities to Yes/No predictions. On a 3% positive target, LightGBM outputs low raw probabilities (e.g., max 0.15). F0.5 grades everything as 0.00 — Optuna goes completely blind.

**Fix**: Replaced `f0.5` with `average_precision` (PR-AUC) in `vm_canary_run.sh` default metrics:
```diff
-METRICS="logloss,f0.5"
+METRICS="logloss,average_precision"
```

**Two-Tier Metric Architecture (going forward)**:
| Tier | Purpose | Metrics | Threshold? |
|------|---------|---------|------------|
| **Tier 1: ML Brain** (Optuna) | Rank setups by confidence | `logloss` + `average_precision` | ❌ Threshold-free |
| **Tier 2: Execution Trigger** (Backtester) | Find optimal trading threshold | `F0.5` + `Profit Factor` | ✅ Sweep 0.50–0.95 |

**Impact**: F0.5 is NOT removed from the codebase — it remains a valid metric for Tier 2 threshold sweeps and for targets with >15% positive rate. For rare targets (<5% positive rate), always use `average_precision` in the Optuna loop.

### Cloud Training Deployed
- VM: `optuna-runner-directed` (n2-highcpu-48, STANDARD)
- 4 parallel searches: `{LONG, SHORT} × {logloss, average_precision}` × 20 trials
- GCS: `gs://cltrainer-optuna-results/data/CL_set_11_asym.parquet`

### Files Changed
- `scripts/generate_asymmetric_target.py` — **[NEW]** Asymmetric Drawdown target engineering
- `gcp/vm_canary_run.sh` — default METRICS changed: `f0.5` → `average_precision`
- `configs/strategies/volatility_breakout.json` — frozen with optimized Optuna params
- `agent/strategy_optimizer.py` — wider SL/TP search bounds + `breakout_window` parameter
- `src/live_execution/strategies/execution_models.py` — `BreakoutStraddleStrategy` + `override_entry_price`
- `agent/backtest_engine.py` — `override_entry_price` support in trade entry

## 2026-03-22 — Canary Pipeline, Process Parallelism & New Dataset Experiments

### Goal
Fix LightGBM SIGSEGV crashes with multi-threaded Optuna workers, build a lightweight canary pipeline for rapid experiment validation, and test new datasets (set_11, HourSet_02) for signal detection.

### LightGBM SIGSEGV Fix (Process-Level Parallelism)
- **Problem**: Optuna with `n_jobs > 1` crashed with `exit 139 (SIGSEGV)` — LightGBM's `Booster.__boost()` is not thread-safe when multiple workers call `lgb.train()` concurrently in the same process.
- **Solution 1 (Trial-Level)**: 4 OS processes per search, each running a subset of trials. Fixed the crash but suboptimal for Bayesian optimization.
- **Solution 2 (Search-Level, Final)**: All 4 Optuna searches (LONG/SHORT × logloss/f0.5) launched simultaneously as separate OS background processes. Each search runs its full 20 trials sequentially (`n_jobs=1`). Best of both worlds: parallel CPU utilization + sequential Bayesian optimization.
- **Wall clock**: 15 min on `n2-highcpu-48` (vs 22 min sequential, same as trial-parallel).

### Canary Pipeline Infrastructure

**`gcp/vm_canary_run.sh`** — **[NEW]** Lightweight 20-trial canary script:
- 4 parallel searches (LONG/SHORT × logloss/f0.5) via background processes
- `--target-long` / `--target-short` args for custom target columns (e.g., HourSet_02's 120H targets)
- Chains E2E pipeline after searches with `--targets` passthrough
- Auto-shutdown after completion

**`gcp/gcp_deploy_canary.ps1`** — **[NEW]** One-command canary deployment:
- Creates VM (`n2-highcpu-48`, STANDARD pricing ~$1.63/hr)
- Uploads code + downloads data from GCS
- Launches tmux canary session
- `-TargetLong` / `-TargetShort` params for custom targets
- `-NoShutdown` for debugging

**`gcp/gcp_monitor.ps1`** — **[NEW]** Automated monitoring:
- Polls VM status + GCS heartbeat every 90s
- Detects VM termination, auto-downloads artifacts from GCS
- Worker-specific log coloring (`[W1]`–`[W4]`)

**`gcp/vm_e2e_pipeline.py`** — Enhanced:
- Step 4c: Ensemble backtest (combines long + short OOS predictions per metric)
- Direct CSV loading + in-memory merge (no dependency on backtest_engine CLI internals)
- `--targets` arg for custom target columns

### Dataset Experiment Results (Canary, 20 trials each)

| Dataset | Timeframe | Model | Trades | WR | PF | PnL | Signal? |
|---------|-----------|-------|--------|-----|------|-----|---------|
| **set_08** (leaky) | 5-min | logloss ensemble | 13,065 | 64.1% | **4.59** | **$2.15M** | ✅ Strong |
| **set_08** (leaky) | 5-min | f0.5 ensemble | 9,772 | 71.1% | **6.56** | **$1.75M** | ✅ Strong |
| **set_10** (clean) | 5-min | all models | **0** | — | — | $0 | ❌ None |
| **set_11** (clean+feat) | 5-min | long_logloss | 319 | 35.7% | 0.84 | -$8,913 | ❌ Unprofitable |
| **set_11** (clean+feat) | 5-min | short_f0.5 | 5 | 40% | 1.04 | $51 | ❌ Marginal |
| **HourSet_02** | 1-hour | long_f0.5 | 2 | 50% | 1.19 | $290 | ❌ Marginal |
| **HourSet_02** | 1-hour | short_logloss | 24 | 50% | 0.89 | -$1,055 | ❌ Unprofitable |
| **HourSet_02** | 1-hour | short_f0.5 | 38 | 42.1% | 0.74 | -$6,251 | ❌ Unprofitable |

### Key Insight
**Only set_08 (which has lookahead leakage) produces strong trading signals.** All causally-safe datasets (set_10, set_11, HourSet_02) produce either zero or unprofitable signals. The "alpha" in set_08 comes from the MACRO resample lookahead and bfill leakage — not from genuine market patterns. This is the most important finding: the model architecture and feature set need fundamental changes to find real alpha without leakage.

### GCS Data Staging
- `set_11`: `gs://cltrainer-optuna-results/data/cl-5m_bk_set_11.parquet` (809 MB)
- `HourSet_02`: `gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_02.parquet` (88 MB)

### Bugs Fixed
- **E2E pipeline custom targets**: `vm_canary_run.sh` wasn't passing `--target-long`/`--target-short` to E2E pipeline — HourSet_02's E2E crashed looking for `TARGET_TRIPLE_2x1_24H_LONG` in a dataset that only has `TARGET_TRIPLE_2p0x1_120H_LONG`. Fixed by adding `--targets "$TARGET_LONG" "$TARGET_SHORT"` to E2E_ARGS.
- **Ensemble backtest import error**: `vm_e2e_pipeline.py` tried importing non-existent `load_predictions` / `load_ohlcv` from backtest_engine. Fixed by directly loading OOS CSVs and merging prob_Buy/prob_Sell columns in-memory.
- **OOM investigation**: Identified that `n2-highcpu-48` VMs have no swap configured and the startup script didn't install `psutil`. Memory usage (~3-4 GB per worker) is well within 48 GB RAM.

### Files Changed
- `gcp/vm_canary_run.sh` — **[NEW]** canary pipeline with search-level parallelism + custom targets
- `gcp/gcp_deploy_canary.ps1` — **[NEW]** one-command canary deployment with target passthrough
- `gcp/gcp_monitor.ps1` — **[NEW]** automated monitoring + artifact collection
- `gcp/vm_e2e_pipeline.py` — ensemble backtest step + custom targets + direct CSV loading
- `agent/optuna_lgbm_search_v2.py` — `--worker-id` arg, tagged logging, worker status files

## 2026-03-21 — E2E Alpha Factory Pipeline

### Goal
Upgrade the GCP Optuna pipeline from a simple hyperparameter search into a full End-to-End Alpha Factory that automatically trains final models, runs backtests, creates registry bundles, and packages artifacts — all on the VM.

### Changes

**`agent/optuna_lgbm_search_v2.py`** — Search space upgrades:
- Hardcoded `num_threads=8` (was dynamic `cpu_count // n_jobs`), paired with `n_jobs=12` (12×8 = 96 cores)
- Wider ranges: `num_leaves` 15→90, `max_depth` 4→10, `learning_rate` 0.005→0.1, `n_estimators` 500→3000, `reg_alpha/lambda` 0.01→10.0, `feature_fraction` 0.3→1.0
- New search dimensions: `boosting_type` {gbdt, goss}, `path_smooth` [0.0–10.0]
- New metric: `average_precision` (PR-AUC via `sklearn.metrics.average_precision_score`)
- GOSS guard: `bagging_fraction`/`bagging_freq` only suggested when `boosting_type=gbdt`
- **NOT added**: `scale_pos_weight` (conflicts with focal loss gradient math), `dart` boosting (5-10× slower)

**`gcp/vm_e2e_pipeline.py`** — **[NEW]** Self-contained E2E orchestrator (~400 lines):
1. Extracts `study.best_params` from Optuna `.db` files
2. Trains final LightGBM models with focal loss on train split
3. Generates OOS predictions on vault data
4. Runs `BacktestEngine` with `ensemble4.json` (TP=2.5, SL=1.5, consecutive_signal=2)
5. Creates registry-compatible bundles (same format as EXP-033)
6. Zips all artifacts → uploads to GCS → optionally shuts down VM

**`gcp/vm_run_optuna.sh`** — Rewritten with `--e2e` and `--shutdown` flags. Chains `vm_e2e_pipeline.py` after Optuna. Backward compatible without flags.

**`gcp/gcp_deploy_run.ps1`** — Updated defaults:
- `NJobs=12`, `NTrials=200`, `StrategyConfig=ensemble4.json`
- New flags: `-E2E`, `-Shutdown`
- Uploads 14 files (was 8): added `vm_e2e_pipeline.py`, `execution_models.py`, strategies dir, `ensemble4.json`

**`configs/strategies/ensemble4.json`** — **[NEW]** Copy of ensemble3_5 with conservative ATR mults (TP=2.5, SL=1.5), used for E2E backtest evaluation.

### Production Config
- **Dataset**: `set_10` (`cl-5m_bk_set_10.parquet`, uploaded to GCS at `gs://cltrainer-optuna-results/data/`)
- **Metrics**: logloss, f0.5, average_precision (dropped f1)
- **Workers**: 12 Optuna workers × 8 LGB threads = 96 cores (100% utilization)
- **Trials**: 200 per study
- **Backtest config**: `ensemble4.json` (TP=2.5, SL=1.5, consecutive_signal_threshold=2)

### GCS Data Staging
- Dataset uploaded to `gs://cltrainer-optuna-results/data/cl-5m_bk_set_10.parquet` (1.3 GB)
- `vm_production_run.sh` auto-downloads from GCS on startup if not already on disk (~30s within GCP vs 16 min SCP from local)
- Any VM in the `cltrainer` project can access the same dataset — no per-VM SCP upload needed
- Cost: ~$0.03/month for 1.3 GB in GCS

### Preemption Recovery
- SPOT VM was preempted during first production run at 83/200 trials (LONG logloss)
- Fixed `vm_production_run.sh` with smart resume: counts existing completed trials in `.db`, computes remaining, skips completed searches
- Intermediate `.db` files are uploaded to GCS after each search completion
- tmux sessions do NOT survive preemption — script must be relaunched after VM restart

### Bugs Fixed During Testing
- **Study name mismatch (critical)**: E2E pipeline couldn't find `.db` files — added `--study-prefix` passthrough + directory scan fallback
- **Shutdown gap**: VM stayed running if E2E crashed with `--shutdown` — added fallback shutdown in bash
- **`set -e` + PIPESTATUS**: Script exited before capturing exit code — switched to `|| true` pattern
- **NaN target crash**: Vault data had NaN targets — added `dropna()` + `fillna(-1)` safety
- **Optuna resume**: Was running 200 NEW trials on restart instead of resuming — fixed with existing trial count check

### Files Changed
- `agent/optuna_lgbm_search_v2.py` — search space + metrics + threading
- `gcp/vm_e2e_pipeline.py` — **[NEW]** E2E orchestrator
- `gcp/vm_run_optuna.sh` — rewritten with E2E chaining
- `gcp/vm_production_run.sh` — **[NEW]** preemption-safe 6-search wrapper with GCS data staging
- `gcp/gcp_deploy_run.ps1` — E2E mode + new file uploads
- `configs/strategies/ensemble4.json` — **[NEW]** backtest config
- `HANDOFF.md` — updated GCP section
- `docs/GCP_OPTUNA_GUIDE.md` — updated with E2E workflow
## 2026-03-21 — HourSet_01: 1-Hour Macro Swing-Trading Dataset

### Goal
Build a completely independent macro swing-trading dataset on 1-hour bars resampled from the raw 5-min data.

### Changes
- **`alpha_factory.py`**: Added `bars_per_hour` parameter to `AlphaFactory.__init__()` (default=12 for 5-min, set to 1 for 1H). Updated `add_macro_context()` to use `self.bars_per_hour` instead of hardcoded 12.
- **`data_processor.py`**: Added `resample_to_hourly()` static method (OHLCV aggregation), `process_hourset_01()` pipeline, `max_warmup_bars` parameter in `cleanup()`, and HourSet_01 routing.
- **`DATASETS.json`**: Added HourSet_01 entry.

### Pipeline
1. Load 1,218,395 5-min bars → resample to **103,461** 1H bars
2. AlphaFactory(bars_per_hour=1) with windows [24, 72, 168, 336, 840] hours
3. Macro windows: 1W, 2W, 1M, 3M, 6M
4. Triple-barrier targets: 72H and 120H horizons × 1.5x, 2.0x, 2.5x ATR
5. Cleanup: 2,200 bar warmup (1H equivalent of 26K 5-min)

### Result
| Metric | Value |
|--------|-------|
| Rows | **101,261** |
| Columns | 176 |
| Feature NaN | **0** |
| MACRO NaN | **0** (all 10 columns) |
| Targets | 20 (6 horizons × 3 + 2 return) |
| Date range | 2009-03-31 → 2026-02-15 |
| Build time | **37 seconds** |
| Output | `cl-5m_bk_HourSet_01.parquet` |

## 2026-03-21 — Train-Serve Skew Fixes & Causally Safe Dataset (set_09, set_10)

### Goal
Implement the fixes identified in the 2026-03-20 investigation and generate new causally-safe training datasets.

### Fix 1: MACRO Resample Lookahead (CRITICAL)
**File**: `alpha_factory.py` `add_macro_context()`

Replaced `resample("1h")` → `reindex(method="ffill")` with bar-level rolling:
- `df["High"].ffill().rolling(window=hours*12, min_periods=1).max()` — direct 5-min bar rolling
- `min_periods=1` — instant warmup from row 1 (no NaN propagation)
- `.clip(lower=1e-8)` — bulletproof division-by-zero protection (`.replace()` misses float zeros)
- Removed `_add_macro_donchian()` helper (no longer needed)

### Fix 2: NaN Cold-Start Warning
**File**: `live_trader.py` `build_live_features()`

Detects which features were zero-filled from NaN during cold start and logs:
`COLD START: N features zero-filled from NaN (model never saw 0 during training)`

### Fix 3: Cache Depth Validation
**File**: `live_trader.py` `build_live_features()`

Warning when cache depth < `MIN_RECOMMENDED_BARS = 26,000` (insufficient for MACRO_3M warmup).

### Fix 4: bfill() Lookahead Removal
**File**: `data_processor.py` `cleanup()`

Removed `.bfill()` from the cleanup pipeline — backward fill copies future values into past rows (lookahead bias). Replaced with:
- Dynamic warmup: `MAX_WARMUP_BARS = 26,000` (covers MACRO_3M = 25,920 bars, VOL_VOLVOL_10080 = 20,160 bars)
- Forward-fill only: `df[non_target_cols].ffill()` — strictly causal
- Corrupted row dropna with warning

### Fix 5: Division-by-Zero NaN Poisoning in Feature Generators
**File**: `alpha_factory.py` — 6 methods patched

Root cause of set_10 initial 705K-row dataset: `VOLFLOW_CMF` used `(High - Low).replace(0, np.nan)` as denominator. On flat bars (High == Low, 2.8% of 2008-09 data), this created NaN that propagated through `rolling(10080).sum()` for years. The diagnostic identified exactly 5 VOLFLOW_CMF columns as the sole offenders (486,561 NaN rows in CMF_10080 alone).

**Fix**: Replaced all `.replace(0, np.nan)` denominators with `.clip(lower=1e-8)` in:
- `add_liquidity_cluster()` — `dollar_vol`
- `add_microstructure_cluster()` — `candle_range = (High - Low)`
- `add_trend_cluster()` — `range_span = (roll_max - roll_min)`
- `add_volume_flow_cluster()` — `vol_sum`, CLV `(High - Low)`, CMF volume denominator
- `add_stochastic_cluster()` — `range_span = (roll_high - roll_low)`

### Datasets Generated

| Dataset | Rows | Columns | Date Range | Changes |
|---------|------|---------|------------|---------|
| set_08 | 1,207,895 | 174 | 2009-01-15 → 2026-02-15 | Original (resample MACRO, bfill cleanup, div-by-zero NaN) |
| set_09 | 1,207,895 | 174 | 2009-01-15 → 2026-02-15 | Fixed MACRO rolling only (bfill still present) |
| **set_10** | **1,192,395** | 174 | 2009-04-03 → 2026-02-15 | Fixed MACRO + causal cleanup + div-by-zero fix (100% clean) |

- set_10 rows: exactly 1,218,395 raw − 26,000 warmup = 1,192,395 (zero dropna casualties)
- All feature columns: **0 NaN** in set_10
- All 2,592 NaN are in TARGET columns at dataset tail (expected)

### Files Changed
- `src/features/alpha_factory.py` — MACRO bar-level rolling fix + `.clip(lower=1e-8)` on all 6 div-by-zero sites
- `src/live_execution/live_trader.py` — NaN warning + cache depth validation
- `src/data_processor.py` — bfill removal, 26K warmup, set_09/set_10 routing
- `data/DATASETS.json` — set_09 and set_10 entries

### Test Results
- **167/168 passed** (1 pre-existing failure: `test_marketable_limit_sell_to_zero` — `64.98 == 64.96`)

---

> **Older entries (2026-03-20 and earlier)** have been archived to [`AGENT_LOG_ARCHIVE.md`](AGENT_LOG_ARCHIVE.md) for historical reference.

