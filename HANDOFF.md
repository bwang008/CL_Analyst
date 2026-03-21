# HANDOFF

## Current Branch
- `main`

## Last Completed Task
- **E2E Alpha Factory Pipeline (2026-03-21)**: Upgraded GCP Optuna pipeline from simple hyperparameter search to full E2E Alpha Factory. Wider search ranges, `boosting_type` (gbdt/goss), `path_smooth`, `average_precision` metric. New `vm_e2e_pipeline.py` auto-trains final models, runs BacktestEngine, creates registry bundles, zips + uploads to GCS. Uses `ensemble4.json` for backtests. Ready for production launch on `set_09`.

## Current Known Bugs / Issues
- **MACRO resample lookahead bias (CRITICAL)**: `alpha_factory.py` `add_macro_context()` uses `resample("1h")` which leaks up to 55 minutes of future data in training (complete hourly bars get forward-filled to 5-min resolution). `MACRO_POS_1M` is EXP-032's #1 feature. Fix: replace with bar-level rolling windows. Must retrain after fix.
- **NaN fill mismatch (MODERATE)**: Training drops NaN rows via `dropna()`, live uses `fillna(0)`. Model never saw 0-filled features during training. Low risk currently (cache depth is sufficient) but latent bug during cold start.
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
| Dataset | Features | EXHAUST | Notes |
|---------|----------|---------|-------|
| set_06 | 82 | 0 | Original |
| set_07 | 141 | 0 | Added extended features |
| set_08 | 156 | 15 | Added exhaustion features |
| **set_09** | **156** | **15** | **MACRO lookahead fix (causally-safe bar-level rolling)** |
| **set_10** | **TBD** | **TBD** | **Latest production dataset** |

## Data File Requirements
- **Shared raw data**: The CL seed CSV (`cl-5m_bk.csv`, ~72 MB) is **not stored in git** (too large). Set `CL_DATA_ROOT` env var to the shared data folder. Code falls back to `data/raw/cl-5m_bk.csv` if unset.
- **GCS dataset staging**: Processed datasets are uploaded to `gs://cltrainer-optuna-results/data/`. VMs auto-download from GCS on startup (~30s within GCP). Current dataset: `cl-5m_bk_set_10.parquet` (1.3 GB).
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

### Metrics
| Metric | Description |
|---|---|
| `logloss` | Best-calibrated probabilities |
| `f0.5` | F-Beta emphasizing precision |
| `average_precision` | PR-AUC — ranking confident positive signals |

### Quota Notes
| Quota | Current | Needed |
|-------|:-------:|:------:|
| CPUS_ALL_REGIONS | 100 | 96 ✅ |
| N2_CPUS | 100 | 96 ✅ |
| C3_CPUS | 8 | N/A (not used) |

## Immediate Next Steps
- **Launch production E2E run on set_09** — 3 metrics × 2 directions = 6 Optuna studies (200 trials each)
- **Download and compare results** — pick best metric per direction, create new ensemble config
- **Harmonize NaN fill handling** between training and live pipelines
- Address `trailing_atr_mult = 0.0` bug (should disable trailing stop, currently triggers immediately)
