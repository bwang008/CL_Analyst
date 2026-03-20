# HANDOFF

## Current Branch
- `main`

## Last Completed Task
- **Panama Canal Rollover & Cache Backup (2026-03-19)**: Replaced destructive cache rebuild with Panama Canal non-destructive back-adjustment. Contract rollovers now shift all OHLC by the median roll delta instead of deleting and re-seeding. Added `data/cache_backups/` with timestamped snapshots on every rollover. 422/429 tests pass.

## Current Known Bugs / Issues
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

## Data File Requirements
- **Shared raw data**: The CL seed CSV (`cl-5m_bk.csv`, ~72 MB) is **not stored in git** (too large). Set `CL_DATA_ROOT` env var to the shared data folder. Code falls back to `data/raw/cl-5m_bk.csv` if unset.
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

## GCP Cloud Deployment (Active)

**VM**: `optuna-runner` (`c2d-highcpu-56`, 56 vCPUs, `us-central1-a`)
**Guide**: `docs/GCP_OPTUNA_GUIDE.md`
**Scripts**: `gcp/` directory

### Quick Start (for agents)
```powershell
# 1. Create VM (if not running)
.\gcp\gcp_setup.ps1

# 2. Deploy code + data + launch search
.\gcp\gcp_deploy_run.ps1 -DataPath "C:\CL_Analyst_Data\data\processed\CL_set_08.parquet"

# 3. Check status
.\gcp\gcp_check_status.ps1

# 4. Download results + delete VM
.\gcp\gcp_teardown.ps1
```

### Key Architectural Decisions
- `optuna_lgbm_search_v2.py` has **inlined** experiment log helpers (no `experiment_runner.py` import)
- Only **8 Python files** uploaded to VM (not full project)
- `tmux` session survives SSH disconnections
- Results auto-upload to GCS (`gs://cltrainer-optuna-results`)
- `pandas_ta` removed from GCP deps (not needed for Optuna search)

### Quota Notes
| Quota | Current | Needed |
|-------|:-------:|:------:|
| CPUS_ALL_REGIONS | 100 | 56+ ✅ |
| C2D_CPUS | 100 | 56 ✅ |
| C3_CPUS | 8 | 88 ❌ (request increase for future) |

## Immediate Next Steps
- Complete Optuna smoke test on GCP VM and verify result download to local
- Complete EXP-025/026 retrain to regenerate OOS predictions → backtest ensemble2_alt for comparison with ensemble3
- Run remaining bake-off metrics (f1, f0.5, sharpe) on winning dataset (see `docs/EXPLORATION_BACKLOG.md`)
- Address `trailing_atr_mult = 0.0` bug (should disable trailing stop, currently triggers immediately)
