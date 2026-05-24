# AGENT_LOG — Archive (Pre-March 21, 2026)

> This file contains archived AGENT_LOG entries that are no longer in the active log.
> Kept for historical reference. The active log is in `AGENT_LOG.md`.
> Archived entries are in reverse-chronological order.

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
- `scripts/diagnose_data_health.py` — **[NEW]** standalone data health diagnostic
- `.agents/workflows/diagnose.md` — added data health check as step 1

## 2026-03-13 — Model Investigation, Bug Fixes & Feature Importance Tooling

### Bugs Fixed
1. **Archive path bug** (`experiment_runner.py`): Archival used hardcoded `models/final_model.pkl` instead of experiment-specific isolated output. Fixed + staleness guard added.
2. **SingleModelStrategy threshold** (`execution_models.py`): Ignored `models.{direction}.threshold` and `entry_threshold`, hardcoded 0.45. Fixed.
3. **Case-insensitive prediction columns** (`backtest_engine.py`): Added `_resolve_prob_column()`.
4. **Silent column substitution** (`backtest_engine.py`): Now raises `ValueError`.
5. **PerformanceWarning spam** (`alpha_factory.py`): Suppressed pandas fragmentation warnings.

### New Tools
- **`scripts/extract_feature_importance.py`** — CLI tool to extract feature importance from any PKL in the registry.
- **Auto-extraction in `archive_model.py`** — PKL → feature_importance.csv on archive.

### Ensemble3_3 (Current Best Config)
- EXP-033 (LONG, set_08) + EXP-032 (SHORT, set_08) with `ConservativeEnsembleStrategy`
- **$2,657,674 PnL**, 4.01 PF, 46.1% WR, ~14,200 trades, -$6,566 MDD over 50 months

### Prediction Distribution Analysis
- `scripts/plot_prediction_distributions.py` — diagnostic tool for probability distributions
- **EXP-025** confirmed entirely compressed (max=0.547, 0% actionable signals)
- All Optuna models (EXP-030 through EXP-033) have healthy distributions with 20-26% above 0.60

## 2026-03-13 — GCP Cloud Deployment for Optuna Searches

### Goal
Run Optuna hyperparameter searches on GCP high-CPU VMs instead of local i9 (16-24 cores). Reduces 16+ hour local runs to ~2-3 hours.

### Scripts Created (`gcp/`)
| Script | Runs On | Purpose |
|--------|---------|---------|
| `gcp_setup.ps1` | Local (PowerShell) | Creates VM + GCS bucket, installs deps (~3 min) |
| `gcp_deploy_run.ps1` | Local (PowerShell) | Uploads 8 essential files + data, launches tmux search |
| `gcp_check_status.ps1` | Local (PowerShell) | Check progress, attach tmux, download .db mid-run |
| `gcp_teardown.ps1` | Local (PowerShell) | Downloads results + deletes VM |
| `vm_startup.sh` | VM (bash) | Boot-time installer: Python venv + ML packages |
| `vm_run_optuna.sh` | VM (bash) | Tmux runner: executes Optuna, auto-uploads to GCS |

### Optuna v2 Searches on Set_08
- **Long model**: 126 trials, best trial #86: logloss=-0.564705
- **Short model**: 106 trials, best trial #91: logloss=-0.559181
- EXP-031 Long backtest: **$1,551K PnL, 3.45 PF, 44.2% WR**
- EXP-032 Short backtest: **$694K PnL, 1.53 PF, 34.3% WR**
- Ensemble3: **$2,600K PnL, 3.88 PF, 45.9% WR**

## 2026-03-11 — EXP-030 Logloss Bake-off & Registry-Centric Pipeline

- **Optuna search**: 119 trials on `CL_set_07`, best trial #114
- **OOS backtest** (2022-01 → 2026-02): **$1.67M PnL, 2.96 PF, 41.9% WR, 10,427 trades**
- Made `models/registry/{EXP_ID}/` the single source of truth for experiments
- Dual-model auto-merge in backtest engine (outer join, eliminates manual merge step)

## 2026-03-10 — Set 08: Exhaustion Features

- Added 15 `EXHAUST_*` features (exhaustion cluster) to AlphaFactory
- `CL_set_08.parquet` — 1,207,895 rows × 174 columns (154 features, +15 vs set_07)

## 2026-03-10 — Entry Order TTL (1-Bar Cancel)

- Added `_check_entry_order_ttl()` to cancel unfilled entry orders after 1 bar
- Prevents position guard from permanently blocking new signals

## 2026-03-10 — Live Trader TieredEnsemble Support & Per-Order Overrides

- TieredEnsembleStrategy: per-tier TP/SL/trailing/max_hold via `Order` dataclass
- Live trader parity: per-trade overrides for trailing stop and time barrier
- `TieredEnsemble2.json`: 2 long tiers + 2 short tiers with distinct risk params

## 2026-03-03 — Resubscription Bug Fixes (BUG-005, BUG-006)

- **BUG-005**: Async resubscription crash — fixed with `reqHistoricalDataAsync()` + `_deferred_resubscribe()`
- **BUG-006**: Invalid `contract=` kwarg in gap backfill — removed

## 2026-03-02 — Live Trader Bug Fixes & Parity Tester Enhancement

- **BUG-001**: Bar subscriptions lost after IBKR disconnect — added 2104/2106 trigger codes
- **BUG-002**: Marketable limit orders crash — replaced live NBBO fetch with bar close + 2 ticks
- **BUG-003**: Timezone mismatch in resubscription — fixed tz-naive/tz-aware subtraction
- **BUG-004**: TWS mobile blocks paper bot — IBKR limitation, not fixable in code
- Position sizing (lots) implemented in BacktestEngine + ConfigurableStrategy
- Live paper trading session: **+$2,991.56** (3 short trades)

## 2026-03-02 — Entry Order Upgrade: Adaptive Algo + Marketable Limit

- Three entry modes: `adaptive` (default), `marketable_limit`, `market`
- IBKR Adaptive Algo for mid-spread improvement

## 2026-02-24 — Track 4.4: Smart Backfill & Dual-Ledger (Live Execution Engine)

- Three-Tier data architecture in `data_manager.py` (seed → cache → IBKR backfill)
- Two-Stream architecture: Brain (continuous contract) + Hands (front-month)
- IBKR connectivity verified: paper account DU1899929

## 2026-02-23 — Track 2.1: Short Sniper (panic-selling)

- EXP-020 (`S_Ultimate_Short`), threshold 0.60: **70.0% WR, PF 2.39, +$1,071,745**

## 2026-02-23 — Track 1: Reality Check (market friction + OOS stress)

- Friction-aware vault run: **PF 7.76, 84.4% WR, +$1,277,168**
- OOS regime testing: COVID window profitable but degraded

## 2026-02-23 — Model Registry system

- Added `models/registry/` system and `agent/archive_model.py` CLI
- Archived EXP-017 bundle
