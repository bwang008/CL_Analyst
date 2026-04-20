# HANDOFF (Agent Bootstrap & Context)

> Quick-read bootstrap file for new agent sessions. Read this FIRST for instant project context.

## Project
Crude oil (CL) 5-minute bar ML trading system using LightGBM with focal loss, walk-forward validation, and IBKR live execution.

## Current State (2026-03-29)
- **Live Trader Subsystem**: Hardened observability for after-hours market tracking via heartbeat logging. Fixed the lean pipeline logic to directly compute Stochastics and DayOfWeek when requested (required for the new `production_lean_dual` model). Production `.pkl` models are now tracked in git.
- **Hourly Optimal Models**: Found that `TARGET_TRIPLE_2p5x1_120H_SHORT` (logloss) is the most robust companion model on `HourSet_02` (26 trades, 61.5% WR, PF 1.58, +$6k) to pair with the existing 72H Long model.
- **Production model**: EXP-037 `LeanMomentumShort` on `set_11c_lean` — 208 trades, 31.2% WR, PF 1.27, **+$5,816**. First statistically significant profitable clean model.
- **Production config**: `configs/strategies/production_lean_momentum.json` — SHORT only, threshold 0.60, TP=3.5x, SL=1.5x, Brain=CL, Hands=MCL.
- **Feature Bucket Architecture**: Implemented. Momentum = alpha. Structure/trend = noise. Divergence = toxic on set_12.
- **Metric policy**: Optuna uses `logloss` + `average_precision` (PR-AUC). Do NOT use `f0.5` in Optuna for targets with <5% positive rate.
- **Bucket flag**: use `--use-buckets` (CLI) / `-UseBuckets` (PowerShell) for 150-trial bucket-enabled runs.

## Root Documentation — Reading Priority

| File | Read? | Purpose |
|------|:-----:|---------|
| **`HANDOFF.md`** (this file) | ✅ Always | 30-second orientation, technical reference, configs, known bugs |
| **`AGENT_LOG.md`** | ⚡ Skim | Last 2-3 days of detailed work history. Skim the headers for context |
| **`AGENT_LOG_ARCHIVE.md`** | ❌ Skip | Historical entries older than ~3 days. Deep debugging only |
| **`INVESTIGATION_RESULTS.md`** | ❌ Skip | 2026-03-20 skew investigation (bugs are fixed). Cross-reference only |
| **`READBEN.me`** / `README.md` | ❌ Skip | High-level project overview — agents have better context here |

## Key Operational Files

| File | Purpose |
|------|---------|
| `experiment_tracker.json` | Structured registry of all experiments with metrics |
| `research_backlog.json` | Prioritized queue of experiment ideas to try |
| `models/registry/` | Archived model bundles (PKL + metrics + predictions) |
| `models/production/` | **Production model PKL** (lean momentum short) |
| `configs/strategies/` | Live trading and backtest strategy configs |
| `.agents/workflows/` | Slash commands — run `/sweep-ensembles`, `/run-tests`, `/commit`, `/next`, etc. |

### Documentation Maintenance Protocol
To keep the AI context window sharp, all agents must follow these rules before ending a session:
1. **Log your work**: Add a new `## YYYY-MM-DD — Title` entry at the top of `AGENT_LOG.md` (under the header) summarizing what you did, bugs fixed, and metric results.
2. **Update HANDOFF**: Update the "Last Completed Task", "Current Known Bugs", and "Immediate Next Steps" sections in this file (`HANDOFF.md`).
3. **Auto-Prune**: If `AGENT_LOG.md` exceeds ~500 lines (or contains more than 5-7 days of history), automatically move the oldest entries to the top of `AGENT_LOG_ARCHIVE.md`. Leave a pointer at the bottom of `AGENT_LOG.md` referencing the archive. Do not ask the user for permission to prune; just do it to keep the active log clean.


## Datasets
| Dataset | Status | Notes |
|---------|--------|-------|
| set_06, 07, 08 | ⛔ Leaked | MACRO resample lookahead + bfill + div-by-zero |
| set_09 | ⚠️ Partial | Fixed MACRO, still has bfill |
| **set_10** | ✅ Clean | Causally safe, 1.19M rows, 156 features |
| **set_11** | ✅ Clean | Latest 5-min: set_10 + new features, 199 columns |
| **set_11c** | ✅ Clean | Corwin-Schultz spread-adjusted. Near-breakeven (EXP-035). Profitable at 0.56 threshold (EXP-037b) |
| **set_11c_lean** | ✅ Clean | **🏆 PRODUCTION.** 26 features (core+momentum). EXP-037: 208 trades, PF 1.27, +$5,816 |
| **set_12** | ✅ Clean | Cumulative: 227 features + TARGET_VOL_EXPANSION. EXHDIV divergence = toxic |
| **HourSet_02** | ✅ Clean | Latest hourly: 1H bars, 120H targets, ~101K rows |
| **CL_set_11_asym** | 🔬 Expr. | set_11 + Asymmetric targets (3.46% LONG / 3.23% SHORT) |

---

## Current Branch
- `live_trader_test` (merged from `development` 2026-03-21)

## Last Completed Task
- **HourSet_02 Canary Experiments (2026-03-29)**: Ran 4 Canary experiments targeting 1.5x, 2.0x, and 2.5x ATR shorts on 120H horizons. Identified `TARGET_TRIPLE_2p5x1_120H_SHORT` (Canary 4, logloss) as the optimal short companion model. Created `/sweep-ensembles` workflow to automate Cartesian combinations.
- **Lean Canary Breakthrough & Production Deployment (2026-03-27)**: Threshold sweep found profitability at 0.56 (+$1,176 on 26 trades). Lean canary (26 features) validated "less is more" hypothesis: **EXP-037 short_logloss = 208 trades, PF 1.27, +$5,816**. Deployed production model (`models/production/`), config (`configs/strategies/production_lean_momentum.json`), and code changes for MCL routing + lean feature path.
- **Feature Bucket Architecture & Winning Strategy Optimization (2026-03-26)**: Implemented feature bucket pruning (12 buckets, 11 toggleable) in Optuna. Ran 3 bucket canary experiments (EXP-034 through EXP-036). Key finding: **momentum = alpha** (ON in 10/12 searches), **structure = noise** (OFF in 11/12).

## Current Known Bugs / Issues
- **~~MACRO resample lookahead bias~~** ✅ FIXED (2026-03-21): Replaced `resample("1h")` with bar-level `rolling(hours*12, min_periods=1)` + `ffill()` + `.clip(lower=1e-8)`.
- **~~NaN fill mismatch~~** ✅ MITIGATED (2026-03-21): Added cold-start zero-fill warning in `live_trader.py`. Training pipeline now uses ffill-only (no bfill).
- **~~bfill() lookahead in cleanup()~~** ✅ FIXED (2026-03-21): Removed `.bfill()`, increased warmup from 10,500 to 26,000 bars.
- **~~1H/4H DataManager cold-bootstrap corruption~~** ✅ FIXED (2026-04-20): `seed_path_1h` pointed at `cl-1h_bk.csv` (never existed). DataManager silently fell back to IBKR bootstrap, producing a thin 142-bar cache instead of the full 3,600-bar HourSet_02 parquet. Fixed: seed now points at `cl-1h_bk_HourSet_02.parquet`; DataManager now supports parquet seeds; hard-fail added if seed is missing (no more silent bootstrap).
- **`trailing_atr_mult = 0.0` bug**: Setting this to 0 does not disable the trailing stop as expected — it triggers immediately. Workaround: set to a large value (e.g. 99.0) to effectively disable.
- **Evaluator naming**: `reports/vault_metrics.json` uses class names `{1: "Buy", 2: "Sell"}` even for binary short targets. For `TARGET_TRIPLE_2x1_24H_SHORT`, the "Buy" slot corresponds to the positive short label.
- **Binary probabilities**: With focal loss custom objective, LightGBM `predict()` may emit logits (not 0-1). The live trader applies sigmoid transform; use `agent/threshold_sweep_binary.py` (sigmoid-aware) for binary sweeps.
- **Data coverage**: processed datasets start at `2009-01-15…` in `set_06`; true 2008-era OOS requires earlier data coverage in processed parquet.
- **Optuna `--n-jobs 3` on Windows**: SQLite locking causes crashes around 20-50 trials. Use `--n-jobs 2` on Windows. Error log goes to `models/optuna_studies/{study_name}_errors.log`.
- **Live-backtest parity gap**: Live predictions may differ significantly from OOS predictions. Old `ensemble2_alt` was spamming sells every bar (live prob much higher than OOS). Monitor with `scripts/plot_prediction_distributions.py`.

## Execution Strategies (Critical Rules)
All strategies are in `src/live_execution/strategies/execution_models.py`.

| Strategy | `execution_class` | Threshold Source | Behavior |
|---|---|---|---|
| **SingleModelStrategy** | _(default when unset)_ | `models.{direction}.threshold` → `entry_threshold` → **1.0 (no trades + warning)** | Single direction only |
| **ConservativeEnsembleStrategy** | `"ConservativeEnsembleStrategy"` | `models.long.threshold` / `models.short.threshold` | Dual-model, no position flipping |
| **AggressiveEnsembleStrategy** | `"AggressiveEnsembleStrategy"` | Same as Conservative | Dual-model WITH position flipping |
| **TieredEnsembleStrategy** | `"TieredEnsembleStrategy"` | Per-tier `min_prob` | Per-tier TP/SL/trailing overrides |

> **IMPORTANT**: Strategies ONLY decide when to enter and in which direction. All trade management (TP/SL/trailing/cooldown/time barrier) is handled by `BacktestEngine` from the config. Strategies are purely signal filters.

> **IMPORTANT**: Prediction column matching is case-insensitive (via `_resolve_prob_column` in `backtest_engine.py`). In dual-model merge, if a long CSV has no 'buy' column or short CSV has no 'sell' column, a `ValueError` is raised — no silent fallback.

## Strategy Config Structure
Configs are in `configs/strategies/`. Reference: `configs/strategies/config_readme.md`.

**Single-model** (manatee.json, koala.json):
```json
{
    "nickname": "Manatee", "direction": "LONG",
    "entry_threshold": 0.60, "tp_atr_mult": 3.0, "sl_atr_mult": 1.5,
    "sizing_tiers": {"0.80": 3, "0.70": 2, "0.60": 1},
    "live_config": {"experiment_id": "EXP-017_S_Ultimate", "client_id": 10}
}
```

**Ensemble** (ensemble3_3.json — current best):
```json
{
    "nickname": "Ensemble3_3",
    "execution_class": "ConservativeEnsembleStrategy",
    "models": {
        "long": {"experiment_id": "EXP-033_optuna_v2_set08_154feat_logloss", "predictions_path": "models/registry/.../oos_predictions.csv", "threshold": 0.60},
        "short": {"experiment_id": "EXP-032_optuna_v2_set08_short_logloss", "predictions_path": "models/registry/.../oos_predictions.csv", "threshold": 0.60}
    },
    "sizing_tiers": {"0.80": 3, "0.70": 2, "0.60": 1},
    "live_config": {"client_id": 18}
}
```

> **Note:** `predictions_path` is used by `backtest_engine.py` (auto-resolve predictions from config). `experiment_id` is used by the live trader to load models from the registry.

## Client ID Assignments
| Config | client_id |
|--------|-----------|
| manatee.json | 10 |
| koala.json | 11 |
| manatee_single.json | 12 |
| ensemble_conservative.json | 13 |
| ensemble2_alt.json | 15 |
| manatee3.json | 16 |
| koala3.json | 17 |
| ensemble3.json / ensemble3_3.json | 18 |
| ensemble3_3_conservative.json | 20 |

## Feature Importance
- **Auto-extracted during archive**: `archive_model.py` now auto-extracts `feature_importance.csv` from the PKL Booster object when no CSV is explicitly provided.
- **Manual extraction**: `python scripts/extract_feature_importance.py EXP-ID --top 20 --filter EXHAUST --save`
- **PKL format**: Models are stored as `{"model": <Booster>, "feature_names": [...], "n_features_in_": int, "params": {...}}`. Use `model.feature_importance(importance_type='gain')` for Booster, `model.feature_importances_` for LGBMClassifier.

## Dataset Feature Counts
| Dataset | Features | Timeframe | Notes |
|---------|----------|-----------|-------|
| set_06 | 82 | 5-min | Original |
| set_07 | 141 | 5-min | Added extended features |
| set_08 | 156 | 5-min | Added exhaustion features — **HAS LOOKAHEAD LEAKAGE** |
| set_09 | 156 | 5-min | MACRO lookahead fix (causally-safe bar-level rolling) |
| set_10 | 156 | 5-min | Causally safe (bfill removed, 26K warmup) |
| **set_11** | **199** | **5-min** | **Latest: set_10 + new features (no leakage). 809 MB, 1,192,395 rows** |
| **set_11c** | **206** | **5-min** | **Corwin-Schultz spread-adjusted. Best clean model (EXP-035)** |
| **set_12** | **227** | **5-min** | **Cumulative + EXHDIV + TARGET_VOL_EXPANSION. Divergence = toxic.** |
| HourSet_01 | 176 | 1-hour | First hourly dataset, 101K rows |
| **HourSet_02** | **199** | **1-hour** | **Latest hourly: new features, 120H targets. 88 MB, ~101K rows** |

## 🚨 Live Execution Data Pipeline — Non-Negotiable Design Rules

> These rules exist because silent fallbacks in the live trading pipeline create **fake environments** that corrupt data quality and make bugs invisible. Violating any of these rules is a critical defect.

### Rule 1: No Silent Fallbacks — Fail Loudly or Not at All
The `DataManager` and any live data pipeline MUST fail with a hard exception if a required data source is missing. There is no backup, no dummy data, no IBKR bootstrap as a cold-start substitute. If the seed is missing → process raises. If the cache is too thin → process raises.

```python
# WRONG — silent degradation that corrupts the system:
if not seed.exists():
    log.warning("Seed missing, bootstrapping from IBKR...")
    self._df = dummy_row

# CORRECT — hard fail:
if not seed.exists():
    raise FileNotFoundError(f"Seed file missing: {seed}. Fix the path or restore the file.")
```

### Rule 2: Validate Minimum Bars at Startup — Before Inference Runs
After warm-start, verify `len(rolling_df) >= min_required_bars` for the active model. Raise immediately. Do not let feature generation be the first thing that discovers insufficient data.

| Bar Size | Min 1H Bars Required |
|---|---|
| `1h` | 840 |
| `2h` | 840 (resamples to 420 2H-bars) |
| `4h` | 840 (resamples to 210 4H-bars) |

### Rule 3: Lock Seed Paths to Explicit Verified Files
Never derive seed paths from naming conventions or assume files exist because nearby files exist. Use `get_data_root()` and specify the **exact filename**. Verify the path resolves before deploying.

```python
# WRONG — assumed pattern, never verified:
seed_path_1h = str(Path(seed_path).parent / "cl-1h_bk.csv")

# CORRECT — explicit, verified path:
seed_path_1h = str(get_data_root() / "processed" / "cl-1h_bk_HourSet_02.parquet")
```

### Rule 4: One Pipeline — No Redundancy That Masks Failures
Live data flows: `seed file → cache → live append`. IBKR backfill is for bridging **recent gaps only** (hours), never as a cold-start data source. There is no Plan B.

### Rule 5: Path Audit Before Deploying New Timeframes
Whenever a new bar size is added, verify all paths resolve and have minimum bars before the first live run. A path audit script (`audit_paths.py`) should be created and run as part of this check.

---

## Data File Requirements

- **Shared raw data**: The CL seed CSV (`cl-5m_bk.csv`, ~72 MB) is **not stored in git** (too large). `CL_DATA_ROOT` is **required**; `src/data_paths.py` raises if it is missing. Place `data/raw/cl-5m_bk.csv` under the `CL_DATA_ROOT` tree (or mirror `data/` into the repo and set `CL_DATA_ROOT` to that parent).
- **GCS dataset staging**: Processed datasets are uploaded to `gs://cltrainer-optuna-results/data/`. VMs auto-download from GCS on startup (~30s within GCP). Latest datasets: `cl-5m_bk_set_11.parquet` (809 MB), `cl-1h_bk_HourSet_02.parquet` (88 MB).
- **Model registry**: `models/registry/` **tracked by git** and appears in worktrees/clones.
- **Processed data** (`data/processed/*.parquet`): Generated by `DataProcessor`. Not in git — regenerate as needed.

## Contract Rollover Handling (Panama Canal)
- **Automatic**: On startup, `DataManager.initialize()` detects front-month changes via `.roll_metadata.json` and applies a Panama Canal back-adjustment.
- **How it works**: Fetches 3-day IBKR overlap, computes median Close delta, shifts all OHLC in cache + master ledger by that delta.
- **Roll history**: Stored in `.roll_metadata.json` with `roll_history[]` and `cumulative_delta`.
- **Negative prices**: Deep historical prices may go negative after many rolls. This is expected — LightGBM uses relative features (ATR, MACD, returns) that are unaffected.
- **Fallback**: `_full_rebuild_cache()` exists for catastrophic recovery but is limited by IBKR's ~60-day 5-min bar history.

## Cache Backups
- **Location**: `data/cache_backups/` (tracked by git)
- **Trigger**: Every contract rollover + first run (when no backups exist)
- **Contents**: Timestamped `warm_start_cache_<ts>_<reason>.parquet` + `roll_metadata_<ts>_<reason>.json`
- **Size**: ~2.5 MB per snapshot, ~5 MB/year growth
- **Recovery**: Copy any backup parquet → `warm_start_cache.parquet` in the data root

## GCP Cloud Deployment (Active — E2E Alpha Factory)

**VM**: `optuna-runner` (`n2-highcpu-96`, 96 vCPUs, `us-central1-a`, SPOT pricing ~$1.08/hr)
**Guide**: `docs/GCP_OPTUNA_GUIDE.md`
**Scripts**: `gcp/` directory

### Quick Start (E2E Mode)
```powershell
# 1. Create VM (if not running)
.\gcp\gcp_setup.ps1

# 2. Deploy code + set_09 data + launch E2E pipeline (Optuna → train → backtest → package)
.\gcp\gcp_deploy_run.ps1 `
    -DataPath "C:\CL_Analyst_Data\data\processed\cl-5m_bk_set_09.parquet" `
    -Target "TARGET_TRIPLE_2x1_24H_LONG" `
    -MlMetric logloss -E2E

# 3. Check status
.\gcp\gcp_check_status.ps1

# 4. Download results + delete VM
.\gcp\gcp_teardown.ps1
```

### Canary Quick Start (Rapid 20-Trial Validation)
```powershell
# 1. Deploy canary with default 5-min targets
.\gcp\gcp_deploy_canary.ps1 -ProvisioningModel STANDARD `
    -GcsDataPath "gs://cltrainer-optuna-results/data/cl-5m_bk_set_11.parquet"

# 2. Deploy canary with custom hourly targets
.\gcp\gcp_deploy_canary.ps1 -ProvisioningModel STANDARD `
    -GcsDataPath "gs://cltrainer-optuna-results/data/cl-1h_bk_HourSet_02.parquet" `
    -TargetLong "TARGET_TRIPLE_2p0x1_120H_LONG" `
    -TargetShort "TARGET_TRIPLE_2p0x1_120H_SHORT"

# 3. Monitor (auto-downloads results when VM terminates)
.\gcp\gcp_monitor.ps1 -VmName optuna-runner-canary -GcsPrefix canary
```

### E2E Pipeline Flow
```
Optuna search (200 trials, 12 workers × 8 threads = 96 cores)
    ↓
vm_e2e_pipeline.py extracts best_params from .db
    ↓
Trains final LightGBM models (focal loss, downsample)
    ↓
Generates OOS predictions on vault data
    ↓
Runs BacktestEngine with ensemble4.json config
    ↓
Creates registry-compatible bundles + production_artifacts.zip → GCS
```

### Key Files Uploaded to VM
- `agent/optuna_lgbm_search_v2.py` — Optuna search (wider ranges, boosting_type, path_smooth, average_precision)
- `gcp/vm_e2e_pipeline.py` — **[NEW]** E2E orchestrator (train + backtest + package)
- `gcp/vm_run_optuna.sh` — tmux runner (chains E2E after Optuna via `--e2e` flag)
- `agent/backtest_engine.py` + `src/live_execution/strategies/` — BacktestEngine + execution models
- `configs/strategies/ensemble4.json` — backtest strategy config (TP=2.5, SL=1.5, consecutive_signal=2)

### Search Space (E2E Alpha Factory Edition)
| Parameter | Range |
|---|---|
| `boosting_type` | {gbdt, goss} |
| `num_leaves` | 15–90 |
| `max_depth` | 4–10 |
| `learning_rate` | 0.005–0.1 |
| `n_estimators` | 500–3000 |
| `feature_fraction` | 0.3–1.0 |
| `bagging_fraction` | 0.3–1.0 (GBDT only) |
| `reg_alpha/lambda` | 0.01–10.0 |
| `path_smooth` | 0.0–10.0 |
| `min_child_samples` | 20–300 |

> **NOT searched**: `scale_pos_weight` (conflicts with focal loss), `dart` boosting (5-10× slower)

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

> **⚠️ CRITICAL (2026-03-23)**: Do NOT use `f0.5` in Optuna for targets with <5% positive rate. F0.5 uses a hard 0.50 threshold — on a 3% target, all probabilities fall below 0.50 and Optuna goes blind (every trial returns 0.00). Use `average_precision` instead.

### Quota Notes
| Quota | Current | Needed |
|-------|:-------:|:------:|
| CPUS_ALL_REGIONS | 100 | 96 ✅ |
| N2_CPUS | 100 | 96 ✅ |
| C3_CPUS | 8 | N/A (not used) |

## Available Datasets
| Dataset | Rows | Date Range | MACRO | Cleanup | Leakage | Canary Result |
|---------|------|------------|-------|---------|---------|---------------|
| set_08 | 1,207,895 | 2009→2026 | resample ❌ | bfill ❌ | **YES** | ✅ PF 4.59, $2.15M |
| set_09 | 1,207,895 | 2009→2026 | rolling ✅ | bfill ❌ | Partial | Not tested |
| set_10 | 1,192,395 | 2009→2026 | rolling ✅ | ffill ✅ | None | ❌ 0 trades |
| **set_11** | **1,192,395** | 2009→2026 | rolling ✅ | ffill ✅ | None | ❌ PF 0.84, -$8.9K |
| HourSet_01 | 101,261 | 2009→2026 | rolling ✅ | ffill ✅ | None | Not tested |
| **HourSet_02** | **~101K** | 2009→2026 | rolling ✅ | ffill ✅ | None | ❌ PF 0.74, -$6.3K |

> **CRITICAL FINDING (2026-03-22)**: All "alpha" in set_08 comes from lookahead leakage. Every leak-free dataset produces zero or unprofitable signals with the current model architecture (LightGBM + focal loss + current feature set). The model architecture and/or feature engineering need fundamental changes to find real alpha.

## Immediate Next Steps
1. **Threshold sweep on EXP-035 (set_11c long_logloss)**: 34% WR / PF 0.98 is 4% above breakeven — sweep threshold 0.50→0.65 to generate more trades
2. **Momentum Core hypothesis**: Test a lean 19-feature model using only `MOM_*` + `core` features
3. **Vol Expansion + Direction combo**: Two-stage model (predict expansion, then direction)
4. Read `experiment_tracker.json` for what's been tried
5. Read `research_backlog.json` for prioritized ideas

### Current Active Tasks
- **✅ COMPLETE: Bucket Architecture** — 3 runs done (EXP-034–036). Best: EXP-035 set_11c (PF 0.98)
- **Next: Threshold sweep** on set_11c long_logloss model to unlock profitability
- Address `trailing_atr_mult = 0.0` bug (should disable trailing stop, currently triggers immediately)
