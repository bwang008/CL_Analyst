# HANDOFF (Agent Bootstrap & Context)

> Quick-read bootstrap file for new agent sessions. Read this FIRST for instant project context.

## Project
Crude oil (CL) **hourly bar** ML trading system using LightGBM with focal loss, walk-forward validation, and IBKR live execution via WSL.

> **Historical note**: The project originally used 5-minute bars (`set_XX` datasets). All `set_XX` references (set_06 through set_12) refer to deprecated 5-minute models. Lookahead leakage was discovered in set_08 and earlier — those datasets produced ~$5M+ backtested profit that was entirely from data corruption. The leakage was fixed and the project moved to hourly (`HourSet_XX`) datasets which are now the only active development line.

## Current State (2026-06-03)
- **Live Trading**: Running on WSL via `setsid`, connected to IBKR on port 4002 (clientId 1010).
  ```bash
  python -m src.live_execution.live_trader --config configs/strategies/HourSet_08_Ensemble_03_05242026.json --host 127.0.0.1 --port 4002
  ```
- **Next Deployment Target**: `HS09_Ensemble_E01_06032026.json` — HourSet_09 ensemble with TieredEnsembleStrategy. $98K PnL (less than other models but selected for high trade count, low drawdown volatility).
  ```bash
  python -m src.live_execution.live_trader --config configs/strategies/HS09_Ensemble_E01_06032026.json
  ```
- **Feature Pipeline**: `AlphaFactory` generates ~200+ features per window from OHLCV. Includes volatility (Parkinson, Rogers-Satchell, Yang-Zhang), liquidity (Amihud, Corwin-Schultz), structure (efficiency ratio, Hurst, entropy), momentum (RSI, BB, ADX, PPO), macro context, trend, volume flow, exhaustion, return distribution, cross-timeframe ratios, term structure shapes, Ichimoku, and DMA clusters.
- **Execution Strategies**: TieredEnsembleStrategy is the primary strategy class. Supports per-direction tier configs with independent TP/SL/trailing/ATR/cooldown parameters.
- **Global Execution Guard**: Config-driven trade blocking for toxic hours and long-weekend transitions. Applied automatically via `configs/global_risk_filters.json` unless `override_global_filters: true`.

## Root Documentation — Reading Priority

| File | Read? | Purpose |
|------|:-----:|---------| 
| **`HANDOFF.md`** (this file) | ✅ Always | 30-second orientation, technical reference, configs, known bugs |
| **`AGENT_LOG.md`** | ⚡ Skim | Last 2-3 days of detailed work history. Skim headers for context |
| **`AGENT_LOG_ARCHIVE.md`** | ❌ Skip | Historical entries older than ~3 days. Deep debugging only |
| **`READBEN.me`** / `README.md` | ❌ Skip | High-level project overview — agents have better context here |

## Key Operational Files

| File | Purpose |
|------|---------|
| `experiment_tracker.json` | Structured registry of all experiments with metrics |
| `research_backlog.json` | Prioritized queue of experiment ideas to try |
| `models/registry/` | Archived model bundles (PKL + metrics + predictions) |
| `configs/strategies/` | Live trading and backtest strategy configs |
| `.agents/workflows/` | Slash commands -- `/sweep-ensembles`, `/run-tests`, `/commit`, `/next`, `/analyze-trade-patterns`, `/run-cloud-experiment`, etc. |
| `configs/global_risk_filters.json` | Global execution guard rules (auto-inherited by all strategies) |
| `src/live_execution/execution_guard.py` | ExecutionGuard class (hour/holiday blocking logic) |
| `src/live_execution/config_loader.py` | Centralized config loader with global filter inheritance |
| `src/live_execution/data_manager.py` | 4-tier data pipeline: seed → cache → IBKR backfill → master ledger |
| `src/features/alpha_factory.py` | Feature generation engine (~200+ features) |
| `src/live_execution/feature_pipeline.py` | Live feature generation wrapper (cache → AlphaFactory → inference) |

### Documentation Maintenance Protocol
To keep the AI context window sharp, all agents must follow these rules before ending a session:
1. **Log your work**: Add a new `## YYYY-MM-DD — Title` entry at the top of `AGENT_LOG.md` summarizing what you did, bugs fixed, and metric results.
2. **Update HANDOFF**: Update the "Last Completed Task", "Current Known Bugs", and "Immediate Next Steps" sections in this file.
3. **Auto-Prune**: If `AGENT_LOG.md` exceeds ~500 lines, automatically move oldest entries to `AGENT_LOG_ARCHIVE.md`. Do not ask permission.

## Datasets (Active — Hourly Only)

| Dataset | Features | Rows | Status | Notes |
|---------|----------|------|--------|-------|
| **CL_HourSet_08** | ~200 | ~101K | ✅ Production | Current live model training data. Seed for warm_start_cache_1h. |
| **HourSet_09** | ~250+ | ~101K | ✅ Canary | Latest: adds Ichimoku, DMA, Term Structure Shape features. Active GCP batch. |
| HourSet_02–07 | varies | ~101K | 🔬 Historical | Earlier hourly iterations. HourSet_02 was the first clean hourly dataset. |

> **5-minute datasets (DEPRECATED)**: `set_06` through `set_12` were 5-minute bar datasets. `set_08` and earlier had MACRO resample lookahead bias that inflated backtested PnL to ~$5M+. This was fixed in set_09 (MACRO) and set_10 (bfill removed). All 5-minute development has been discontinued in favor of hourly models.

---

## Current Branch
- `development` (primary active branch on both Windows and WSL)

## Last Completed Task
- **Live Feature Parity Audit & Amihud Fix (2026-06-03)**: Discovered zero-volume IBKR bars (overnight/weekend) caused `clip(lower=1e-8)` in AlphaFactory's Amihud formula to explode `LIQ_AMIHUD_*` features by 10 orders of magnitude (~18 billion vs training mean of 0.02). This compressed all live model probabilities to a tight band around 0.50, making the short model unable to ever cross its threshold. Fixed with NaN guard (`dollar_vol = raw_dv.where(raw_dv > 0, np.nan)`). 562/562 tests pass.
- **Data Pipeline Audit (2026-06-03)**: Verified warm_start_cache integrity — 0 duplicates, 0 nulls, 0 High<Low violations. All 146 non-1H gaps are explainable (CME maintenance, holidays). Cache depth (4,126 bars) sufficient for all active features.

## Current Known Bugs / Issues
- **`trailing_atr_mult = 0.0` bug**: Setting this to 0 does not disable the trailing stop — it triggers immediately. Workaround: set to a large value (e.g. 99.0).
- **MACRO_DXY_CHG_1D stuck at zero**: Live telemetry shows this feature consistently reporting 0.0 across all 166+ observed bars. Likely a stale FRED data feed. Needs investigation.
- **MACRO_YIELD_CURVE_SIGN constant**: Reports 1.0 consistently in live. May be legitimate (yield curve not inverted) or a stale feed.
- **Optuna `--n-jobs 3` on Windows**: SQLite locking causes crashes around 20-50 trials. Use `--n-jobs 2` on Windows.
- **Binary probabilities**: With focal loss custom objective, LightGBM `predict()` may emit logits (not 0-1). The live trader applies sigmoid transform; use `agent/threshold_sweep_binary.py` for binary sweeps.
- **CL/MCL front-month oscillation**: Roll metadata shows occasional CL ↔ MCL (Micro CL) front-month flip, generating unnecessary Panama Canal re-adjustments. Harmless but noisy.

## Execution Strategies (Critical Rules)
All strategies are in `src/live_execution/strategies/execution_models.py`.

| Strategy | `execution_class` | Threshold Source | Behavior |
|---|---|---|---|
| **SingleModelStrategy** | _(default when unset)_ | `models.{direction}.threshold` → `entry_threshold` → **1.0 (no trades + warning)** | Single direction only |
| **ConservativeEnsembleStrategy** | `"ConservativeEnsembleStrategy"` | `models.long.threshold` / `models.short.threshold` | Dual-model, no position flipping |
| **AggressiveEnsembleStrategy** | `"AggressiveEnsembleStrategy"` | Same as Conservative | Dual-model WITH position flipping |
| **TieredEnsembleStrategy** | `"TieredEnsembleStrategy"` | Per-tier `min_prob` | Per-tier TP/SL/trailing overrides. **Primary strategy.** |

> **IMPORTANT**: Strategies ONLY decide when to enter and in which direction. All trade management (TP/SL/trailing/cooldown/time barrier) is handled by `BacktestEngine` from the config. Strategies are purely signal filters.

> **IMPORTANT**: Prediction column matching is case-insensitive (via `_resolve_prob_column` in `backtest_engine.py`). In dual-model merge, if a long CSV has no 'buy' column or short CSV has no 'sell' column, a `ValueError` is raised — no silent fallback.

## Strategy Config Structure
Configs are in `configs/strategies/`. Reference: `configs/strategies/config_readme.md`.

**TieredEnsemble** (current production pattern — `HS09_Ensemble_E01_06032026.json`):
```json
{
    "nickname": "HS09_Ensemble_E01_L1_S1",
    "execution_class": "TieredEnsembleStrategy",
    "bar_size": "1h",
    "models": {
        "long": {"experiment_id": "E2E_HourSet_09_long_logloss", "threshold": 0.53},
        "short": {"experiment_id": "E2E_HourSet_09_short_average_precision", "threshold": 0.53}
    },
    "long": {
        "tiers": [{"min_prob": 0.5, "lots": 1, "tp_atr_mult": 1.5, "sl_atr_mult": 3.5, ...}]
    },
    "short": {
        "tiers": [{"min_prob": 0.53, "lots": 1, "tp_atr_mult": 10.0, "sl_atr_mult": 3.0, ...}]
    },
    "live_config": {"client_id": 1010}
}
```

> **Note:** `predictions_path` is used by `backtest_engine.py` (auto-resolve predictions from config). `experiment_id` is used by the live trader to load models from the registry.

## Client ID Assignments
| Config | client_id | Status |
|--------|-----------|--------|
| HourSet_08_Ensemble_03_05242026.json | 1010 | 🟢 Running on WSL |
| HS09_Ensemble_E01_06032026.json | 1010 | 📋 Next deployment |

> Legacy 5-min client IDs (10–20) are retired. All live trading now uses clientId 1010.

## Feature Importance
- **Auto-extracted during archive**: `archive_model.py` auto-extracts `feature_importance.csv` from the PKL Booster object.
- **Manual extraction**: `python scripts/extract_feature_importance.py EXP-ID --top 20 --filter EXHAUST --save`
- **PKL format**: Models are stored as `{"model": <Booster>, "feature_names": [...], "n_features_in_": int, "params": {...}}`. Use `model.feature_importance(importance_type='gain')` for Booster, `model.feature_importances_` for LGBMClassifier.

## 🚨 Live Execution Data Pipeline — Non-Negotiable Design Rules

> These rules exist because silent fallbacks in the live trading pipeline create **fake environments** that corrupt data quality and make bugs invisible.

### Rule 1: No Silent Fallbacks — Fail Loudly or Not at All
The `DataManager` and any live data pipeline MUST fail with a hard exception if a required data source is missing. No backup, no dummy data, no IBKR bootstrap as a cold-start substitute.

### Rule 2: Validate Minimum Bars at Startup — Before Inference Runs
After warm-start, verify `len(rolling_df) >= min_required_bars` for the active model. Raise immediately.

| Bar Size | Min 1H Bars Required |
|---|---|
| `1h` | 840 |

### Rule 3: Lock Seed Paths to Explicit Verified Files
Never derive seed paths from naming conventions. Use `get_data_root()` and specify the **exact filename**.

### Rule 4: One Pipeline — No Redundancy That Masks Failures
Live data flows: `seed file → cache → live append`. IBKR backfill is for bridging **recent gaps only** (hours), never as a cold-start data source.

### Rule 5: Path Audit Before Deploying New Timeframes
Whenever a new bar size is added, verify all paths resolve and have minimum bars before the first live run.

### Data Pipeline Architecture (4-Tier)
```
Tier 1: Seed parquet (CL_HourSet_08.parquet — 101K rows, used only on first cold start)
    ↓
Tier 2: Warm-start cache (warm_start_cache_1h.parquet — 4,126 rows, persisted between restarts)
    ↓
Tier 3: IBKR live bars (5-min + 1H real-time streams, appended to cache on each bar)
    ↓
Tier 4: Master ledger (cl_continuous_master.parquet — 1.2M 5-min bars, continuous contract)
```

- The **1H cache** seeds from `CL_HourSet_08.parquet` and backfills short gaps from IBKR.
- The **5m master ledger** (`cl_continuous_master.parquet`) is 5-minute only. It is NOT the source for hourly features.
- Cache is **client-agnostic** and **model-agnostic** — stores raw OHLCV only, no features.

---

## Data File Requirements

- **Shared raw data**: `CL_DATA_ROOT` environment variable is **required**; `src/data_paths.py` raises if missing.
- **GCS dataset staging**: Processed datasets uploaded to `gs://cltrainer-optuna-results/data/`. VMs auto-download on startup.
- **Model registry**: `models/registry/` **tracked by git** and appears in worktrees/clones.
- **Processed data** (`data/processed/*.parquet`): Generated by `DataProcessor`. Not in git.

## Contract Rollover Handling (Panama Canal)
- **Automatic**: On startup, `DataManager.initialize()` detects front-month changes via `.roll_metadata.json` and applies a Panama Canal back-adjustment.
- **How it works**: Fetches 3-day IBKR overlap, computes median Close delta, shifts all OHLC in cache + master ledger by that delta.
- **Roll history**: Stored in `.roll_metadata.json` with `roll_history[]` and `cumulative_delta`.
- **Negative prices**: Deep historical prices may go negative after many rolls. LightGBM uses relative features (ATR, returns) that are unaffected.

## Cache Backups
- **Location**: `data/cache_backups/` (tracked by git)
- **Trigger**: Every contract rollover + first run
- **Recovery**: Copy any backup parquet → `warm_start_cache_1h.parquet` in the data root

## GCP Cloud Deployment

**VM**: `optuna-runner` (`n2-highcpu-96`, 96 vCPUs, `us-central1-a`, SPOT pricing ~$1.08/hr)
**Guide**: `docs/GCP_OPTUNA_GUIDE.md`
**Scripts**: `gcp/` directory

### Canary Quick Start (Rapid Validation)
```powershell
# Deploy canary with hourly targets
.\gcp\gcp_deploy_canary.ps1 -ProvisioningModel STANDARD `
    -GcsDataPath "gs://cltrainer-optuna-results/data/cl-5m_bk_HourSet_09.parquet" `
    -TargetLong "TARGET_TRIPLE_3x1_24H_LONG" `
    -TargetShort "TARGET_TRIPLE_3x1_24H_SHORT"

# Monitor (auto-downloads results when VM terminates)
.\gcp\gcp_monitor.ps1 -VmName optuna-runner-canary -GcsPrefix canary
```

### Automated Artifact Routing (Zero-Touch)
When `gcp_monitor.ps1` detects VM completion, it automatically routes:
- `.json` configs to `configs/strategies/`
- `.csv` predictions to `data/predictions/`
- `.pkl` model bundles to `C:\CL_Analyst_Data\models\registry\`

### E2E Pipeline Flow
```
Optuna search (200+ trials, 12 workers × 8 threads = 96 cores)
    ↓
vm_e2e_pipeline.py extracts best_params from .db
    ↓
Trains final LightGBM models (focal loss, downsample)
    ↓
Generates OOS predictions on vault data
    ↓
Runs BacktestEngine with ensemble config
    ↓
Creates registry-compatible bundles + production_artifacts.zip → GCS
```

### Metrics (Two-Tier Architecture)

**Tier 1 — ML Brain (Optuna Search)**:
| Metric | Description | When to Use |
|---|---|---|
| `logloss` | Best-calibrated probabilities | Always |
| `average_precision` | PR-AUC — threshold-free ranking | Always (especially <5% positive targets) |

**Tier 2 — Execution Trigger (Backtest/Threshold Sweep)**:
| Metric | Description | When to Use |
|---|---|---|
| `f0.5` | F-Beta emphasizing precision | Threshold sweep on frozen model |
| `Profit Factor` | Win $ / Loss $ | Backtest evaluation |
| `Sharpe` / `Sortino` | Risk-adjusted returns | Strategy optimization (Optuna Tier 2) |

> **CRITICAL**: Do NOT use `f0.5` in Optuna for targets with <5% positive rate. F0.5 uses a hard 0.50 threshold — on a 3% target, all probabilities fall below 0.50 and Optuna goes blind.

### Script Encoding Rules
**DO NOT use emojis** or multi-byte special characters in `.ps1` scripts. PowerShell 5.1 incorrectly parses UTF-8 files without a BOM. Use bracketed ASCII tags instead (`[COMPLETE]`, `[WARNING]`, `[FAILED]`).

### Quota Notes
| Quota | Current | Needed |
|-------|:-------:|:------:|
| CPUS_ALL_REGIONS | 100 | 96 ✅ |
| N2_CPUS | 100 | 96 ✅ |

## Immediate Next Steps
1. **Deploy HS09 Ensemble**: Switch WSL live trader from `HourSet_08_Ensemble_03` to `HS09_Ensemble_E01_06032026.json` when ready.
2. **Monitor Amihud Fix**: Verify live predictions now produce a wider probability distribution (comparable to OOS backtests) after the zero-volume Amihud fix.
3. **Investigate MACRO_DXY_CHG_1D**: Feed appears stale (constant 0.0). Check FRED data refresh logic.
4. **HourSet_09 Batch Evaluation**: Evaluate remaining E02–E09 ensemble combinations from `batch_20260602_0330`.
