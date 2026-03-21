# AGENT_LOG

Historical progress and completed track summaries (reverse-chronological; newest first).

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

## 2026-03-20 — Train-Serve Feature Skew Investigation

### Goal
Identify all sources of train-serve skew (feature mismatch between training-time and live-inference-time computation) in the CL_Analyst live trading pipeline. Diagnostic-only — no code changes.

### Key Finding: Signal Drop is Market-Driven
The sell signal dropping from 0.99 (Mar 17) to 0.48 (Mar 20) is a **correct model response** to a bullish market regime shift, not a pipeline bug. 99/154 features changed >10% between those timestamps, with short-term trend/momentum/volume features flipping from bearish to bullish. The SHORT model correctly refuses to short in bullish conditions.

### Bug #1: MACRO Resample Lookahead Bias (CRITICAL)
**Location**: `alpha_factory.py` `add_macro_context()` (line 389-413)

`resample("1h")` creates complete hourly bars using all 12 five-minute bars. The 10:00 hourly bar knows H/L through 10:55. `reindex(method="ffill")` assigns this to the 10:05 bar — leaking up to 55 minutes of future data in training. In live, no lookahead exists because the DataFrame ends at the current bar.

**Impact**: MACRO_POS_1M is EXP-032's #1 feature (importance=1032). Leak magnitude: ~4% for MACRO_1D, ~1.4% for 3D, ~0.12% for 1M, ~0.05% for 3M.

**Fix**: Replace hourly resample with causally-safe bar-level rolling windows (e.g., `rolling(840 * 12)` for 1M).

### Bug #2: NaN Fill Mismatch (MODERATE)
**Training** (`data_processor.py` `cleanup()`): Drops first 10,500 warmup rows → `ffill().bfill()` → `dropna()`. Model never sees zero-filled features.

**Live** (`live_trader.py` `build_live_features()`): `ffill() → bfill() → fillna(0)`. Cold-start features get filled with 0, a value the model never saw.

**Current risk**: Low — cache has 35K bars (sufficient). Latent bug if cache is ever corrupted or reset.

### Feature Drift Analysis
Compared 1,233 live telemetry records (Mar 13–20) against 1.2M training records:
- **50/154 features drifted** (live mean outside training [5th, 95th] percentile)
- Dominant pattern: elevated volatility (VOL_ROC_4032 at z=+9.66, VOL_VOLVOL_4032 at z=+7.53)
- **MACRO_POS_1M (#1 feature) is NOT drifted** (z=+0.23) — model's primary input is in-distribution
- Drift is a normal market regime shift, not a pipeline bug

### No Additional Lookahead Found
Full audit of `alpha_factory.py` confirmed:
- Zero `shift(-N)` (no forward-looking shift)
- Zero `rolling(center=True)`
- Zero `resample()` beyond the known MACRO bug
- All `pandas_ta` functions (RSI, ADX, MACD, BBands, ATR) use only past data

### Output
- `INVESTIGATION_RESULTS.md` — full report with severity ratings, data tables, and specific fix instructions

## 2026-03-19 — Panama Canal Rollover & Cache Backup

### Problem
After CLJ6 → CLK6 contract rollover, the live trader's destructive cache rebuild (delete → re-seed from CSV → IBKR backfill) mixed two different back-adjustment bases, causing features to diverge massively. buy_prob/sell_prob collapsed from 0.89 → 0.49, producing zero trade signals.

### Root Cause
IBKR's continuous contract retroactively back-adjusts all historical prices when a roll happens. The old cache had pre-roll prices; the rebuild fetched post-roll prices. Despite the splice being smooth ($0.01 jump), features like ATR_14 doubled, DIST_ZSCORE flipped sign, and VOLFLOW features increased 10x.

### Fix: Non-Destructive Panama Canal Back-Adjustment
Refactored `data_manager.py` to use the institutional-standard Panama Canal method:

| Old (Destructive) | New (Panama Canal) |
|---|---|
| Delete cache on rollover | Keep cache intact |
| Re-seed from CSV (old prices) | Fetch 3-day overlap from IBKR |
| Full IBKR backfill (limited to ~60 days) | Compute median price delta |
| Broke after gap > 60 days | Shift all OHLC by delta — works indefinitely |

**New methods:**
- `_compute_roll_delta()` — returns median Close difference (float) instead of boolean validation
- `_back_adjust_cache(delta)` — shifts all OHLC by delta, overwrites overlap with fresh IBKR data
- `_full_rebuild_cache()` — renamed fallback with warning about IBKR 60-day limit
- `_backup_cache_to_repo()` — timestamped snapshot to `data/cache_backups/` on every rollover + first run

**Updated methods:**
- `initialize()` — uses `_compute_roll_delta()` + `_back_adjust_cache()` instead of validate + rebuild
- `_update_training_ledger()` — applies roll delta to entire 1.2M-row ledger (back to 2008)
- `_save_roll_metadata()` — stores `roll_history[]` and `cumulative_delta` for auditability

### Roll Metadata Format
```json
{
  "last_front_month": "CLK6",
  "cumulative_delta": 0.03,
  "roll_history": [{"from": "CLJ6", "to": "CLK6", "delta": 0.03, "timestamp": "..."}]
}
```

### Cache Backup Feature
- Timestamped snapshots saved to `data/cache_backups/` in the git repo
- Triggers on every contract rollover + first run (when no backups exist)
- Backs up both cache parquet (~2.5 MB) and roll metadata JSON
- File size is negligible (~5 MB/year growth), safe for GitHub

### Test Results
- **22 passed** in `test_rollover.py` (13 new tests: TestComputeRollDelta, TestBackAdjustCache, TestRollMetadataHistory, TestFullRebuildFallback)
- **422 passed** full suite, 7 pre-existing failures (unrelated)

### Files Changed
- `src/live_execution/data_manager.py` — core rollover refactor + backup method
- `tests/test_rollover.py` — replaced 5 old tests with 13 new ones
- `scripts/diagnose_data_health.py` — **[NEW]** standalone data health diagnostic: checks cache integrity, price continuity, feature ranges (ATR_14, Volume_Log), roll metadata, cache backups, telemetry signal health, feature drift detection. Run via `/diagnose` workflow or directly with `python scripts/diagnose_data_health.py --verbose --telemetry`
- `.agents/workflows/diagnose.md` — added data health check as step 1

## 2026-03-13 — Model Investigation, Bug Fixes & Feature Importance Tooling

### Bugs Fixed

1. **Archive path bug** (`experiment_runner.py`): Archival used hardcoded `models/final_model.pkl` instead of experiment-specific isolated output, causing stale models to be archived. Fixed + staleness guard added to `archive_model.py`.
2. **SingleModelStrategy threshold** (`execution_models.py`): Ignored `models.{direction}.threshold` and `entry_threshold`, hardcoded 0.45. Fixed: reads model-specific → `entry_threshold` → defaults to 1.0 (no trades) with loud warning.
3. **Case-insensitive prediction columns** (`backtest_engine.py`): Added `_resolve_prob_column()` for case-insensitive matching (`prob_Buy`, `prob_buy`, etc.). Applied in strategy path, legacy path, and dual-model merge.
4. **Silent column substitution** (`backtest_engine.py`): Dual-model merge silently used `prob_Sell` as `prob_Buy` if no matching column found. Now raises `ValueError`.
5. **PerformanceWarning spam** (`alpha_factory.py`): Suppressed pandas DataFrame fragmentation warnings from 30+ EXHAUST feature column insertions.

### New Tools

- **`scripts/extract_feature_importance.py`** — CLI tool to extract feature importance from any PKL in the registry. Handles dict-wrapped Boosters and LGBMClassifier objects. Options: `--top N`, `--filter EXHAUST`, `--save`, `--all`.
- **Auto-extraction in `archive_model.py`** — When no `feature_importance.csv` is provided during archive, auto-extracts from the PKL. Every archived model now gets a correct, complete feature importance CSV.

### Ensemble3_3 (Current Best Config)
- EXP-033 (LONG, set_08) + EXP-032 (SHORT, set_08) with `ConservativeEnsembleStrategy`
- **$2,657,674 PnL**, 4.01 PF, 46.1% WR, ~14,200 trades, -$6,566 MDD over 50 months (~$53K/month avg)
- Both models have 154 features including 15 EXHAUST features
- Top EXHAUST feature: `EXHAUST_DIST_HIGH_288` ranked #15 (SHORT) / #13 (LONG)

### Key Findings
- **EXP-025 retrain LONG is dead**: max prob=0.547, never crosses 0.60 threshold
- **Feature importance CSVs were truncated**: Registry CSVs showed 80/139 features; actual PKLs contain all 154. Auto-extraction fixes this.
- **Dataset feature counts**: set_06=82, set_07=141, set_08=156 features



### Goal
Quickly identify models with compressed probability distributions (all predictions near 0.50, never reaching the 0.60 trading threshold) versus models with healthy spreads that produce actionable signals.

### Script Created
- `scripts/plot_prediction_distributions.py` — standalone diagnostic tool (no new dependencies beyond scipy/matplotlib/numpy/pandas)

### Features
- **Auto-discovery**: Scans `models/registry/*/oos_predictions.csv`, skips models without prediction files
- **Skip-if-exists**: Won't regenerate PNGs unless `--force` is used
- **Case-insensitive column detection**: Handles `prob_Buy`, `prob_Sell`, `prob_buy`, etc.
- **Per-model individual plots**: Histogram (50 bins) + KDE overlay, color-coded green (≥ threshold) / red (< threshold)
- **Threshold lines**: Primary 0.60 (black dashed) + secondary 0.45 (gray dotted)
- **Stats annotation**: Model name, direction, N, min/max/mean/median, % above threshold, distribution shape
- **Distribution shape classification**: Uses `scipy.signal.find_peaks` + `scipy.stats.skew` to label unimodal/bimodal/skewed
- **Combined comparison grid**: All models in a single 2×3 grid figure
- **CLI**: `--force` (regenerate all), `--threshold` (override primary threshold)
- **Temporal breakdown (2×2 grid per model)**: Signals by hour of day (count + rate), day of week, monthly time series with mean line, year×month heatmap with annotated signal rates. Handles zero-signal models gracefully (EXP-025 shows "No signals above 0.60").

### Key Findings

| Model | Direction | Max Prob | ≥ 0.60 | Shape |
|-------|-----------|----------|--------|-------|
| EXP-025 retrain | LONG | 0.547 | **0.0%** | bimodal |
| EXP-026 retrain | SHORT | 0.931 | 12.1% | unimodal |
| EXP-030 (set_07) | LONG | 0.964 | 25.9% | unimodal |
| EXP-031 (set_08) | LONG | 0.922 | 22.4% | unimodal |
| EXP-032 (set_08 short) | SHORT | 0.940 | 20.9% | unimodal |
| EXP-033 (set_08 154feat) | LONG | 0.945 | 22.1% | unimodal |

- **EXP-025** confirmed entirely compressed (max=0.547, 0% actionable signals) — this model is dead
- All Optuna models (EXP-030 through EXP-033) have healthy distributions with 20-26% above 0.60

### Output
- 6 individual model PNGs + 1 `all_models_comparison.png` in `reports/prediction_distributions/`

## 2026-03-13 — GCP Cloud Deployment for Optuna Searches

### Goal
Run Optuna hyperparameter searches on GCP high-CPU VMs instead of local i9 (16-24 cores). Reduces 16+ hour local runs to ~2-3 hours. Fire-and-forget: launch search, close laptop, results auto-upload to GCS.

### How the VM Lifecycle Works
1. **VM as a cloud computer** — it runs Ubuntu Linux with a terminal/shell just like your PC. You interact via `gcloud compute ssh` (remote shell) or see it in GCP Console → Compute Engine → VM Instances.
2. **tmux for persistence** — the Optuna script runs inside a `tmux` session on the VM. Even if your SSH/internet drops, tmux keeps running. Reconnect anytime with `gcloud compute ssh optuna-runner --command='tmux attach -t optuna'`.
3. **GCS for result safety** — when the search finishes, `vm_run_optuna.sh` auto-uploads results (.db, .json, .csv, logs) to `gs://cltrainer-optuna-results`. Even if the VM is deleted, results persist in GCS.
4. **Console visibility** — the VM appears in GCP Console with status (RUNNING/STOPPED/TERMINATED). You can start/stop it from the console UI too.
5. **Teardown** — `gcp_teardown.ps1` downloads results from VM + GCS, then deletes the VM. The GCS bucket persists for future runs.

### Scripts Created (`gcp/`)

| Script | Runs On | Purpose |
|--------|---------|---------|
| `gcp_setup.ps1` | Local (PowerShell) | Creates VM + GCS bucket, installs deps (~3 min) |
| `gcp_deploy_run.ps1` | Local (PowerShell) | Uploads 8 essential files + data, launches tmux search |
| `gcp_check_status.ps1` | Local (PowerShell) | Check progress, attach tmux, download .db mid-run |
| `gcp_teardown.ps1` | Local (PowerShell) | Downloads results + deletes VM |
| `vm_startup.sh` | VM (bash) | Boot-time installer: Python venv + ML packages |
| `vm_run_optuna.sh` | VM (bash) | Tmux runner: executes Optuna, auto-uploads to GCS |
| `requirements-gcp.txt` | VM | Minimal pip deps (lightgbm, optuna, pandas, numpy, sklearn, pyarrow, sqlalchemy) |

### Key Fixes During Setup
1. **Inlined experiment log functions** into `optuna_lgbm_search_v2.py` — removed `import agent.experiment_runner` which transitively pulled in `main.py`, `data_processor.py`, `pandas_ta`, etc. The 3 functions (`load_experiment_log`, `generate_experiment_id`, `_append_to_log`) are simple JSON helpers now defined inline.
2. **Removed `pandas_ta`** from `requirements-gcp.txt` and `vm_startup.sh` — not needed for Optuna search, caused pip install failure on Ubuntu 22.04.
3. **Fixed venv permissions** — startup script creates venv as root, SSH user needs write access (chmod 777).
4. **Minimal file upload** — `gcp_deploy_run.ps1` uploads only 8 Python files (~130KB) instead of entire project directories (hundreds of trial configs were being uploaded before).
5. **gcloud PATH** — Google Cloud SDK bin dir not in default terminal PATH; scripts auto-detect at `C:\Users\bwang\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin`.
6. **PowerShell string escaping** — backslashes in double-quoted Write-Host strings broke parser; switched to single-quoted strings.

### Quota Issues
- **C3 machines** (c3-highcpu-44/88): quota 0 on free trial, even after "activate full account"
- **N2/E2 spot**: also blocked on quota
- **E2 on-demand**: `e2-highcpu-8` worked (8 vCPUs)
- **Fix**: Request quota increase in GCP Console → IAM & Admin → Quotas → filter by "C3 CPUs"

### Smoke Test
- Launched 2-trial smoke test on `e2-highcpu-8` VM
- Data loaded: 916K rows, 154 features, 10 sampled WF folds
- Optuna started successfully, `smoke_test_v2.db` created
- Full verification pending (interrupted for Google Cloud MCP install)

### Documentation
- Created `docs/GCP_OPTUNA_GUIDE.md` — step-by-step quick start guide with cost estimates, examples, troubleshooting

### Optuna v2 Searches on Set_08

**Long model (set_08):**
- 126 trials, `--n-jobs 2`, study `wf_v2_long_logloss_set08`
- Best trial #86: logloss=-0.564705, F1=0.5945
- Process crashed twice with `--n-jobs 3` (SQLite locking on Windows); stable with `--n-jobs 2`
- Added error logging to `optuna_lgbm_search_v2.py`: try/except + error log file + re-raise

**Short model (set_08):**
- 106 trials, `--n-jobs 2`, study `wf_v2_short_logloss_set08`
- Best trial #91: logloss=-0.559181, F1=0.6055

**Set_07 vs Set_08 A/B comparison (Optuna-level):**
- Logloss: set_07 slightly better (-0.5629 vs -0.5647)
- F1: set_08 slightly better (0.5945 vs 0.5884)
- Conclusion: essentially a tie at Optuna level; set_08 model is more selective (fewer leaves, lower feature_fraction)

### EXP-031: Long Model (Set_08)
- Trained with best Optuna params from trial #86
- Backtest: **$1,551K PnL, 3.45 PF, 44.2% WR, 9,842 trades, -$6,437 MDD**
- Every month profitable
- Compared to EXP-030 (set_07): Less PnL ($-119K) but better PF (3.45 vs 2.96) and WR (44.2% vs 41.9%)

### EXP-032: Short Model (Set_08)
- Trained with best Optuna params from trial #91
- Backtest: **$694K PnL, 1.53 PF, 34.3% WR, 12,199 trades, -$12,052 MDD**
- 6 red months — weaker than the long model

### Ensemble3 (Long + Short Set_08)
- Combined EXP-031 (long) + EXP-032 (short)
- Backtest: **$2,600K PnL, 3.88 PF, 45.9% WR, 14,324 trades, -$7,079 MDD**
- Every month profitable, balanced long/short (7,316 buys / 7,008 sells)

### Strategy Configs Created
- `configs/strategies/manatee3.json` — EXP-031 long (client_id=16)
- `configs/strategies/koala3.json` — EXP-032 short (client_id=17)
- `configs/strategies/ensemble3.json` — Combined (client_id=18)
- `configs/strategies/OPTUNA_EXP-031_Set08.json` — Long backtest config

### EXP-025/026 Retrain (in progress)
- **Why:** Original EXP-025 (long) and EXP-026 (short) predictions were never archived to the registry. Both used `CL_set_06` with pre-Optuna manually-tuned params. Need their OOS predictions regenerated so we can backtest `ensemble2_alt.json` and do a fair comparison with `ensemble3.json`.
- Configs: `configs/experiments/EXP-025_retrain.json`, `configs/experiments/EXP-026_retrain.json`
- Both use identical model params to original (copied from registry `config.json`)

### Pipeline Improvements
- `agent/optuna_lgbm_search_v2.py` — error handling: try/except around `study.optimize()` with traceback logging to `{study_name}_errors.log` and re-raise to stop process
- `docs/EXPLORATION_BACKLOG.md` — new file documenting 6 exploration topics: wider search ranges, metric bake-off, more trials, additional search dims, multi-objective, full walk-forward Optuna

## 2026-03-11 — EXP-030 Logloss Bake-off & Registry-Centric Pipeline

### EXP-030: Optuna v2 Logloss (set_07, Long)

- **Optuna search**: 119 trials on `CL_set_07`, metric=`binary_logloss`, best trial #114
- **Bug fix**: `LGBMLearner.py` — removed `num_class=3` when `use_focal=True` with binary objective (was causing `multiclass objective and metrics don't match` error)
- **Training**: walk-forward, 68 folds, 53.67 min wall time
- **OOS backtest** (2022-01 → 2026-02): **$1.67M PnL, 2.96 PF, 41.9% WR, 10,427 trades, -$6,415 MDD**
- Every month profitable across 4+ years
- Top features: Time_Sin, Time_Cos, STRUC_ENTROPY_100, STRUC_HURST_100, MOM_ADX_14

### Registry-centric pipeline redesign

Made `models/registry/{EXP_ID}/` the single source of truth for experiments.

#### Files changed
- `agent/archive_model.py` — added `oos_predictions_path`, `experiment_config_path`, `feature_importance_path` params
- `agent/experiment_runner.py` — added `--config` flag (reads `configs/experiments/*.json`), auto-calls `archive_model.py` after training
- `agent/backtest_engine.py` — auto-resolves predictions from config's `models.*.predictions_path` when `--predictions` is omitted; dual-model auto-merge (outer join) eliminates manual `merge_predictions.py` step
- `configs/strategies/OPTUNA_EXP-030_Set07.json` — strategy config with `predictions_path` pointing to registry
- `configs/strategies/ensemble2_alt.json` — added `predictions_path` for both long + short models
- `configs/experiments/EXP-030.json` — experiment config template

#### Registry bundle contents (EXP-030)
```
models/registry/EXP-030_optuna_v2_set07_logloss/
  ├── final_model.pkl          # Trained model
  ├── oos_predictions.csv      # OOS predictions (291K rows)
  ├── experiment_config.json   # Training config (for reproduction)
  ├── feature_importance.csv   # Walk-forward feature importance
  ├── config.json              # Experiment metadata
  ├── metrics.json             # Classification + backtest metrics
  ├── vault_metrics.json       # Vault holdout metrics
  └── backtest.csv             # Summary row
```

#### Pipeline flow (new)
```
Optuna search → configs/experiments/EXP-XXX.json
                    ↓
experiment_runner.py --config → trains model → auto-archives to models/registry/EXP-XXX/
                                                    ↓
configs/strategies/OPTUNA_EXP-XXX.json (predictions_path → registry)
                    ↓
backtest_engine.py --config (auto-resolves predictions, auto-merges dual-model)
```

#### Dual-model merge logic
When a strategy config has both `models.long.predictions_path` and `models.short.predictions_path`, the backtest engine:
1. Loads both CSVs independently
2. Extracts `prob_Buy` from long, `prob_Sell` from short
3. Outer-joins on DateTime index, fills NaN with 0.0
4. Runs backtest on the merged DataFrame

This eliminates the manual `scripts/merge_predictions.py` step.

## 2026-03-10 — Set 08: Exhaustion Features

- **Goal**: Add "move exhaustion" features so the model can learn when a directional move is overextended and a snap-back is more likely than continuation. Addresses the problem of the model shorting into violent rebounds during extreme sell-offs (e.g., -14% day, -5% in 15 min).
- **Dataset**: `CL_set_08.parquet` — 1,207,895 rows × 174 columns (154 features, +15 vs set_07).

### New features (AlphaFactory `add_exhaustion_cluster`)

| Feature | Formula | Purpose |
|---------|---------|---------|
| `EXHAUST_CUM_RET_{w}` | `rolling_sum(log_ret, w)` | Cumulative return — session-level directional magnitude |
| `EXHAUST_CUM_ATR_{w}` | `cum_ret × Close / ATR_14` | ATR-normalised exhaustion — scale-invariant "how many ATRs moved" |
| `EXHAUST_DIST_HIGH_{w}` | `(Close - rolling_max) / ATR_14` | Distance from recent high in ATR units (≤ 0) |

Computed at 5 windows: 288, 864, 2016, 4032, 10080 → **15 new features**.

### Files changed
- `src/features/alpha_factory.py` — new `add_exhaustion_cluster()` method, wired into `add_all_features()` under `include_extended=True`
- `src/data_processor.py` — new `process_set_08()` method, `set_08` added to `DATASET_VERSIONS` dict + routing
- `data/DATASETS.json` — set_08 entry added

### Verification
- **22/22** existing tests pass (no regressions)
- All 15 exhaustion features present, zero NaN in feature columns
- `EXHAUST_CUM_RET_288` spot-check: exact match with recomputed `rolling(288).sum(log_ret)`
- `EXHAUST_DIST_HIGH` max = 0.0 (correct: price at rolling high)
- Processing wall time: 56:45


## 2026-03-10 — Entry Order TTL (1-Bar Cancel)

- **Goal**: Cancel unfilled entry orders after 1 bar to prevent the position guard from permanently blocking new signals.
- **Problem**: Bracket entry orders were placed with `tif='GTC'` and no expiry mechanism. If an Adaptive/Limit entry didn't fill, all future signals were blocked indefinitely.
- **Solution**: Added `_check_entry_order_ttl()` to `live_trader.py`:
  - Tracks `_pending_entry_order_id` and `_pending_entry_bar_time` on order placement
  - On each new bar: if 1+ bars elapsed and the entry is still pending (Submitted/PreSubmitted), cancels all CL orders and resets position state
  - Clears pending state on parent fill in `_on_order_status()` to prevent false cancellations
- **Note**: BacktestEngine assumes instant fills — no TTL concept needed there.

## 2026-03-10 — Live Trader TieredEnsemble Support

- **Goal**: Enable `TieredEnsemble2.json` to work with the live trader (`live_trader.py`) by adding per-tier execution parameter support, matching the backtest engine's per-Order override behavior.

### TradeSignal extension (`strategy.py`)
- Added 4 optional per-trade override fields: `tp_atr_mult`, `sl_atr_mult`, `trailing_atr_mult`, `max_hold_bars` (all default `None` for backward compat).

### ConfigurableStrategy tier-awareness (`configurable_strategy.py`)
- 3-mode config detection: **tiered** (`long.tiers`) > **ensemble** (`models`) > **single-model**.
- Tiered mode: parses and sorts tiers by `min_prob` descending, matches first qualifying tier.
- Per-tier: TP/SL multipliers used for bracket computation, trailing/max_hold overrides passed on `TradeSignal`.
- New `_match_tier()` static method.
- Backward compatible: ensemble and single-model configs use unchanged code paths.
- Model loading adapted: tiered configs use `long.experiment_id`/`short.experiment_id` (not `models.long.experiment_id`).

### LiveTrader per-trade overrides (`live_trader.py`)
- **On entry**: stores `signal.trailing_atr_mult` / `signal.max_hold_bars` as `_trade_trailing_atr_mult` / `_trade_max_hold_bars`.
- **`_check_trailing_stop()`**: uses per-trade trailing override when set.
- **`_check_time_barrier()`**: uses per-trade max_hold override when set.
- **`_reset_position_state()`**: resets per-trade overrides to `None`.

### Parity gaps closed
| Feature | Before | After |
|---------|--------|-------|
| Per-tier TP/SL | Backtest only | Backtest + Live |
| Per-tier trailing | Backtest only | Backtest + Live |
| Per-tier max_hold | Backtest only | Backtest + Live |

### Tests
- 72/72 passing (50 backtest_engine + 20 configurable_strategy + 2 others).
- Test stub updated with `_is_tiered`, `_long_tiers`, `_short_tiers`.

### Usage
```bash
# Backtest
python -m agent.backtest_engine --config configs/strategies/TieredEnsemble2.json --predictions reports/oos_predictions.csv

# Live (dry-run)
python -m src.live_execution.live_trader --config configs/strategies/TieredEnsemble2.json --dry-run

# Existing configs still work unchanged
python -m src.live_execution.live_trader --config configs/strategies/ensemble2_alt.json
```

## 2026-03-10 — TieredEnsembleStrategy with Per-Order Overrides

- **Goal**: Implement asymmetric buy/sell tiers with per-tier TP/SL/trailing/max_hold parameters, passed through the `Order` object to the `BacktestEngine`.

### Order dataclass extension
- Added 4 optional per-trade override fields to `Order` in `execution_models.py`: `tp_atr_mult`, `sl_atr_mult`, `trailing_atr_mult`, `max_hold_bars` (all default `None` for backward compat).

### BacktestEngine per-Order overrides
- `_on_flat()`: Accepts `Order`, resolves per-trade TP/SL/trailing/max_hold overrides (falls back to engine globals when `None`).
- `_on_in_position()`: Uses per-trade `_trade_max_horizon` and `_trade_trailing_atr_mult` instead of engine globals.
- `_open_new_position()` + `_check_position()`: `_OpenPosition` extended with `pos_max_horizon` and `pos_trailing_atr_mult` for concurrent mode.
- `_reset_state()`: Initialises per-trade override fields.

### TieredEnsembleStrategy class (~140 lines)
- New class in `execution_models.py`: parses `long.tiers` / `short.tiers` config blocks.
- Tier matching: highest `min_prob` first, first match wins. Each tier specifies lots + TP/SL/trailing/max_hold overrides.
- Conflict resolution: when both buy and sell fire on the same bar, higher probability wins.
- Conservative position management: HOLD when already in position (no-flip).
- Registered in `STRATEGY_REGISTRY`.

### New config
- `configs/strategies/TieredEnsemble2.json`: 2 long tiers (high_confidence @ ≥0.75, base @ ≥0.60) + 2 short tiers (high_confidence @ ≥0.80, base @ ≥0.60), each with distinct TP/SL/trailing/max_hold.

### Backtest results (OOS 2022-01 → 2026-02, 291K bars)

| Config | Total PnL | Win Rate | PF | Trades |
|--------|----------:|:--------:|----:|-------:|
| Koala2_opt (Short only) | $101,650 | 97.6% | 43.57 | 127 |
| Manatee2_opt (Long only) | $293,237 | 78.0% | 39.65 | 132 |
| Ensemble2_Aggro (Both) | $1,080,623 | 62.4% | 2.30 | 7,266 |
| TieredEnsemble2 | $618,520 | 72.8% | 2.12 | 11,974 |

### Documentation
- Updated `configs/strategies/config_readme.md`: added TieredEnsembleStrategy schema, tier matching rules, per-Order overrides section, example config.
- Created `reports/tiered_ensemble_backtest_20260310.md`.

### Test results
- **50 passed, 0 failed** (39 existing + 11 new)
- New test classes: `TestOrderOverrides` (2), `TestTieredEnsembleStrategy` (6), `TestTieredWithEngine` (2)

### Limitations
- **Live trader parity**: The `TieredEnsembleStrategy` is currently **backtest-only**. The live trader's `ConfigurableStrategy` reads the `models` key (not `execution_class`), so per-tier overrides are not yet supported in live trading. A future enhancement would extend `ConfigurableStrategy` to support tiered configs.

## 2026-03-03 — Resubscription Bug Fixes (BUG-005, BUG-006)

### BUG-005: Resubscription crashes with "event loop already running" (bars never recovered)

- **Symptom**: After HMDS disconnect (`Error 10182`) + reconnect (`2106`), resubscription always failed with `RuntimeError: This event loop is already running`. Bars never resumed despite connectivity being restored. Portfolio updates continued, so the bot appeared alive.
- **Root cause**: `_on_ib_error()` is an ib_insync callback running inside the asyncio event loop. `_resubscribe_and_backfill()` calls `subscribe_live_bars()` → `reqHistoricalData()` → `loop.run_until_complete()`, which crashes because the loop is already running. Same async-inside-callback issue as BUG-002 (marketable_limit).
- **Fix (attempt 1 — failed)**: Deferred resubscription via `asyncio.ensure_future()` + `loop.call_soon()`. Still crashed because even in a coroutine, sync ib_insync methods call `loop.run_until_complete()` which fails inside a running loop.
- **Fix (final)**: Added `subscribe_live_bars_async()` to `ibkr_client.py` using `reqHistoricalDataAsync()`. Rewrote `_deferred_resubscribe()` as a fully async method that `await`s the async subscribe. Removed gap backfill from the reconnection path (gaps fill naturally via `keepUpToDate=True`). Added `_resubscribe_pending` flag to deduplicate rapid-fire 2104/2106 events.

### BUG-006: Gap backfill called with invalid keyword argument

- **Symptom**: `TypeError: IBKRConnectionManager.fetch_historical_bars_by_duration() got an unexpected keyword argument 'contract'`
- **Root cause**: `_resubscribe_and_backfill()` passed `contract=self._contract` to `fetch_historical_bars_by_duration()`, but that method doesn't accept a `contract` parameter — it builds its own via `build_cl_contract()`.
- **Fix**: Removed the invalid `contract=self._contract` keyword argument.

### User additions (manual edits)

- Added `cooldown_bars` config param + `_cooldown_remaining` state to `LiveTrader.__init__()`, `_on_new_bar()`, and trade exit handler for parity with backtest engine FSM COOLDOWN state.
- Updated `ensemble_conservative.json`: TP=2.0 ATR, SL=1.0 ATR, sizing tiers all set to 1 contract.
- Updated `ensemble_aggro.json`: SL=3.0 ATR (from 2.5).
- Improved timezone normalization in `_resubscribe_and_backfill()` to handle both tz-aware and tz-naive `_last_bar_time`.

## 2026-03-02 — Live Trader Bug Fixes & Parity Tester Enhancement

### BUG-001: Bar subscriptions lost after IBKR disconnects (never recovered)

- **Symptom**: After `Error 10182` (subscriptions lost), `NEW BAR` and `INFERENCE` logs stopped appearing. `updatePortfolio` continued working. The bot was alive but blind — no new signals generated.
- **Root cause**: `_on_ib_error()` only triggered `_resubscribe_and_backfill()` on error codes 1101/1102 (full connectivity restored). IBKR actually sends warning codes **2104** (Market data farm OK) and **2106** (HMDS data farm OK) after brief disconnects. Resubscription never fired.
- **Fix**: Added 2104 and 2106 to the trigger set in `_on_ib_error()` (`live_trader.py` line ~910).
- **Impact**: Bot now auto-recovers bar subscriptions after brief IBKR data farm blips.

### BUG-002: Marketable limit orders fail with "event loop already running"

- **Symptom**: `[ERROR] Failed to place bracket order: This event loop is already running`. Orders placed with `entry_mode="marketable_limit"` always failed. Position stayed at 0 despite valid sell signals.
- **Root cause**: `marketable_limit` mode called `get_bid_ask()` → `ib.reqTickers()` inside the `_on_new_bar` callback. `reqTickers()` internally calls `loop.run_until_complete()`, but the asyncio event loop is already running (we're inside an ib_insync callback). This is a fundamental ib_insync constraint: only cached/sync methods work inside callbacks.
- **Why it wasn't caught earlier**: The initial short position used `adaptive` mode (default), which only sets order attributes — no async calls. `marketable_limit` was added to the config mid-session and failed on its first real invocation.
- **Fix**: `place_bracket_order()` in `ibkr_client.py` now uses `limit_price` (bar close) + 2 ticks instead of fetching live NBBO via `reqTickers()`. Functionally identical result, no async calls.
- **Safe methods inside callbacks**: `ib.portfolio()`, `ib.positions()`, `ib.openTrades()`, `ib.placeOrder()`, setting order attributes.
- **Unsafe methods inside callbacks**: `ib.reqTickers()`, `ib.accountSummary()`, `ib.reqHistoricalData()` — anything making a new request to IBKR.

### BUG-003: Resubscription crashes with timezone mismatch

- **Symptom**: `TypeError: Cannot subtract tz-naive and tz-aware datetime-like objects` in `_resubscribe_and_backfill()`.
- **Root cause**: `pd.Timestamp.utcnow()` returns tz-aware (`+00:00`), but `self._last_bar_time` is tz-naive (naive UTC). Pandas refuses the subtraction.
- **Why it wasn't caught earlier**: Latent bug — `_resubscribe_and_backfill()` was never executed until BUG-001 was fixed (adding 2104/2106 triggers). The code path was dead until the resubscription trigger fix made it reachable.
- **Fix**: Replaced `pd.Timestamp.utcnow()` with `pd.Timestamp.now(tz="UTC").tz_localize(None)` to produce tz-naive UTC.

### BUG-004: TWS mobile blocks paper bot bar data (IBKR HMDS conflict)

- **Symptom**: `Error 162: Trading TWS session is connected from a different IP address`. No `NEW BAR` logs after startup despite "Subscribed to live bars" message.
- **Root cause**: IBKR's Historical Market Data Service (HMDS) is shared per username across all sessions (Gateway, TWS desktop, TWS mobile). Logging into TWS mobile from a different IP causes HMDS to reject `reqHistoricalData(keepUpToDate=True)` on the Gateway session. Portfolio data and order execution continue working (separate channels).
- **Fix**: Not a code fix — IBKR infrastructure limitation. User must avoid running TWS mobile while the paper bot is running, or use a separate IBKR paper username.

### Enhancement: Strategy filter for validate_parity.py

- **Problem**: Running different strategies (manatee vs ensemble_conservative) appends mixed predictions to the shadow log. `validate_parity.py` replays all rows against one model, causing false `[FAIL] PIPELINE DIVERGENCE`.
- **Solution**: Added `--strategy` filter to `validate_parity.py` and `export_shadow_log.py`. When multiple strategies are detected and no `--strategy` is specified, the script auto-selects the most recent strategy.

### Live trade results (Paper DU1899929, session 2026-03-02)

| # | Direction | Entry | Contracts | Exit | P&L |
|---|-----------|-------|-----------|------|-----|
| 1 | Short @ 72.07 | 02:09 UTC | 4 | SL 72.86 (2), TP 70.10 (2) | +$361.04 |
| 2 | Short @ 71.42 | 15:20 UTC | 2 | TP 70.10 | +$2,630.52 |
| 3 | Short @ 70.62 | 16:50 UTC | 3 | *Open* | — |
| | | | | **Session Total** | **+$2,991.56** |

- **Goal**: Implement position sizing (lots) in BacktestEngine, refactor configs to group live-only attributes under `live_config`, add per-config `client_id`, and make `ConfigurableStrategy` support ensemble configs.

### Config refactoring
- All 4 strategy JSONs (`manatee.json`, `koala.json`, `manatee_single.json`, `ensemble_conservative.json`) restructured: `experiment_id` moved under `live_config`, `client_id` added (10/11/12/13).
- Created `configs/strategies/config_readme.md` — full attribute reference with compatibility matrix.

### Sizing tiers implementation
- `execution_models.py` → `BaseExecutionStrategy._parse_sizing_tiers()` and `_prob_to_lots()`: maps probability to lot count via highest-first matching.
- All 3 strategies (`SingleModelStrategy`, `ConservativeEnsembleStrategy`, `AggressiveEnsembleStrategy`) set `Order.lots` from tiers.
- `backtest_engine.py` → `TradeRecord.lots`, `_OpenPosition.lots`, PnL calculations in `_close_trade` and `_check_position` multiply by `lots`.
- `configurable_strategy.py` → `_prob_to_lots()` mirrors the same logic for live trader parity.

### LiveTrader updates
- CLI `--client-id` default changed from `10` → `1`. Reads `live_config.client_id` from config JSON.
- `ConfigurableStrategy` reads `experiment_id` from `live_config` with backward compat fallback to top-level.
- Refactored `ConfigurableStrategy` for ensemble support: detects `models` dict, loads both LONG and SHORT models, runs dual inference, applies per-model thresholds, higher-probability signal wins on conflict.
- Added `buy_prob`/`sell_prob` fields to `TradeSignal` dataclass.
- Enhanced INFERENCE log: shows direction (LONG/SHORT/BOTH), `buy_prob`, `sell_prob`, and explicit position-skip labeling.
- Fixed shadow state logging to use `signal.buy_prob`/`signal.sell_prob` when available.

### Test results
- **52 passed, 0 failed** (backtest_engine + configurable_strategy tests)
- Updated test stub to handle new ensemble attributes (`_long_learner`, `_short_learner`, `_is_ensemble`, `_long_threshold`, `_short_threshold`).

## 2026-03-02 — Entry Order Upgrade: Adaptive Algo + Marketable Limit

- **Goal**: Upgrade entry orders from bare Market Orders to institutional-grade execution that reduces slippage and captures the spread. Avoid fragile async cancel/replace loops.
- **Solution**: Implemented two new entry modes (configurable), defaulting to IBKR Adaptive Algo.

### New methods
- `ibkr_client.py` → `get_bid_ask(contract)` — real-time NBBO snapshot via `reqTickers()` with 2s timeout and polling. Returns `(bid, ask)` tuple.

### Modified methods
- `ibkr_client.py` → `place_bracket_order()` — new `entry_mode` parameter (`adaptive` / `marketable_limit` / `market`), `adaptive_priority` parameter (`Normal` / `Urgent` / `Patient`). Deprecated `use_market` flag preserved for backward compatibility. Added `TagValue` import for algo params.
- `live_trader.py` → `__init__()` — added `entry_mode` and `adaptive_priority` params.
- `live_trader.py` → `_on_new_bar()` — passes `entry_mode` / `adaptive_priority` to bracket order. Enhanced ORDER PLACED log line shows actual order type (e.g. `LMT+Adaptive`) and entry mode.
- `live_trader.py` → `main()` — added `--entry-mode` and `--adaptive-priority` CLI args. Reads `live_config.entry_mode` / `live_config.adaptive_priority` from strategy JSON. Resolution: CLI > config > default.

### Entry modes

| Mode | Parent Order | Description |
|------|-------------|-------------|
| `adaptive` (default) | LMT + IBKR IBALGO | Server-side algo seeks mid-spread improvement. Zero extra data subscriptions. |
| `marketable_limit` | LMT | Prices 2 ticks ($0.02) through best ask/bid. Falls back to MKT if quote unavailable. |
| `market` | MKT | Legacy behavior. |

### Test results
- **356 passed, 0 failed** (127s)
- `test_bracket_order.py` expanded from 6 → 16 tests across 4 classes: `TestBracketOrderConfig`, `TestAdaptiveAlgoOrder`, `TestMarketableLimitOrder`, `TestEntryModeBackwardCompat`

## 2026-02-24 — Track 4.4: Smart Backfill & Dual-Ledger (Live Execution Engine)

- **Goal**: Solve the "Pipeline Parity Problem" — ensure live trading environment mirrors training environment.
- **Problem**: 5-day cold start was insufficient for indicators with 35-day (10,080 bar) lookback windows.

### New modules
- `src/live_execution/data_manager.py` — Three-Tier data architecture:
  - Tier 1: Immutable seed (reads last 60 days from `data/raw/cl-5m_bk.csv`)
  - Tier 2: Warm-start Parquet cache (`data/processed/warm_start_cache.parquet`)
  - Tier 3: IBKR backfill (gap-fills missing bars) + live append
- `src/live_execution/utils/time_utils.py` — `timedelta_to_ib_duration()` and `split_duration_into_chunks()`

### Modified modules
- `telemetry.py`: Added `raw_front_month_bars` table + `log_raw_bar()`/`raw_bar_count()`/`recent_raw_bars()`
- `ibkr_client.py`: Added `get_front_month_contract()` (resolves CLJ6) + `fetch_historical_bars_by_duration()`
- `live_trader.py`: Replaced `_cold_start()` with `_warm_start()` via DataManager; added Two-Stream architecture:
  - **Brain stream**: Continuous contract → AlphaFactory → model inference → signals
  - **Hands stream**: Front-month contract → bracket orders + raw bar logging to telemetry

### IBKR connectivity verified
- Paper account: `DU1899929`, port `4002` (IB Gateway)
- Front-month: `CLJ6` (conId=304037436, expiry 2026-03-20)
- Mock order: limit buy CL @ $5.00 → Submitted → Cancelled ✓

### Test results
- **174 passed, 0 failed** (44s)
- New tests: `test_data_manager.py` (14), `test_time_utils.py` (15), `test_telemetry.py` (+3 raw bar tests)


## 2026-02-23 — Track 2.1: Short Sniper (panic-selling)

- **Target**: `TARGET_TRIPLE_2x1_24H_SHORT` (binary short label)
- **Experiment**: `EXP-020` (`S_Ultimate_Short`)
- **Dataset**: `data/processed/CL_set_06_shortfix.parquet`
- **Key point**: the short edge was **high-confidence only**; max-F1 overtraded and lost money under friction.

### Threshold sweep + friction results (selected)

- F1-optimal threshold: `0.45`
  - Friction backtest PF: `0.87` (unprofitable; too many trades)
- Selected trade threshold: `0.60`
  - **Win rate**: `70.0%`
  - **Profit factor**: `2.39`
  - **Net PnL**: `+$1,071,745.37` (CL multiplier 1000; commission 2.50/side; slippage 0.03/side)

### Archived bundle

- Registry entry created:
  - `models/registry/EXP-020_S_Ultimate_Short/`
- Catalog updated:
  - `models/registry/README.md`

## 2026-02-23 — Track 1: Reality Check (market friction + OOS stress)

### Task 1.1 — Market friction backtest

- Updated `agent/backtester.py` to support:
  - direction-aware slippage (Buy fills worse; Sell fills worse)
  - flat commission per side
  - CL contract multiplier (1000)
- Friction-aware vault run (S_Ultimate):
  - Profit factor: `7.76`
  - Win rate: `84.4%`
  - Net PnL: `+$1,277,168.30`

### Task 1.2 — OOS regime testing

- Added regime runner `agent/oos_regime_test.py` to train pre-regime and test on extreme windows.
- Findings:
  - 2009 window was skipped due to insufficient pre-window training data in processed set.
  - COVID crash window remained profitable (PF > 1), but materially degraded vs vault-era performance.

## 2026-02-23 — Model Registry system

- Added `models/registry/` system and an archiving CLI:
  - `agent/archive_model.py`
- Archived `EXP-017` bundle:
  - `models/registry/EXP-017_S_Ultimate/`

